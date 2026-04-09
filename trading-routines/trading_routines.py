"""
trading_routines — Automated trading intelligence briefings.

Runs weekly/monthly/quarterly research routines via the Anthropic API with
web search enabled, saves markdown reports, and notifies Telegram.

Designed to run as a long-lived service (APScheduler) or as a one-shot CLI:

    python trading_routines.py run weekly            # manual trigger
    python trading_routines.py run monthly
    python trading_routines.py run quarterly
    python trading_routines.py schedule              # start scheduler daemon
    python trading_routines.py list                  # list available routines
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx
import yaml
from anthropic import Anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Environment-driven settings. Loaded from .env or real env vars."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    max_tokens: int = Field(default=8192, alias="MAX_TOKENS")
    max_web_searches: int = Field(default=10, alias="MAX_WEB_SEARCHES")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    reports_dir: Path = Field(default=Path("reports"), alias="REPORTS_DIR")
    config_file: Path = Field(default=Path("config.yaml"), alias="CONFIG_FILE")
    prompts_dir: Path = Field(default=Path("prompts"), alias="PROMPTS_DIR")

    timezone: str = Field(default="Europe/Paris", alias="TIMEZONE")


# ---------------------------------------------------------------------------
# Routine definitions (loaded from config.yaml)
# ---------------------------------------------------------------------------

class RoutineConfig(BaseModel):
    name: str                           # "weekly" | "monthly" | "quarterly"
    enabled: bool = True
    cron: str                           # APScheduler cron expression (day_of_week, day, etc.)
    prompt_file: str                    # relative to prompts_dir
    telegram_summary: bool = True       # send a summary to Telegram
    summary_max_chars: int = 1500


@dataclass(slots=True)
class BriefingResult:
    routine: str
    started_at: datetime
    finished_at: datetime
    report_md: str
    report_path: Path
    token_usage: dict
    success: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Briefer: calls Claude API with web_search
# ---------------------------------------------------------------------------

class Briefer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = Anthropic(api_key=settings.anthropic_api_key)

    def run_routine(self, routine: RoutineConfig) -> BriefingResult:
        """Synchronous call — the Anthropic SDK handles the heavy lifting."""
        started_at = datetime.now(timezone.utc)
        logger.info(f"Starting routine: {routine.name}")

        prompt_path = self.settings.prompts_dir / routine.prompt_file
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

        prompt = prompt_path.read_text(encoding="utf-8")
        prompt = self._inject_context(prompt, routine)

        try:
            response = self.client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=self.settings.max_tokens,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self.settings.max_web_searches,
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            logger.exception(f"API call failed for routine {routine.name}")
            finished_at = datetime.now(timezone.utc)
            return BriefingResult(
                routine=routine.name,
                started_at=started_at,
                finished_at=finished_at,
                report_md="",
                report_path=Path(),
                token_usage={},
                success=False,
                error=str(e),
            )

        # Extract text blocks from the response (ignore tool_use/tool_result blocks)
        text_parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        report_md = "\n\n".join(text_parts).strip()

        finished_at = datetime.now(timezone.utc)
        token_usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        logger.info(
            f"Routine {routine.name} complete — "
            f"{token_usage['input_tokens']} in / {token_usage['output_tokens']} out tokens"
        )

        # Prepend metadata header
        header = (
            f"# {routine.name.capitalize()} Trading Briefing\n\n"
            f"**Generated**: {finished_at.isoformat()}\n"
            f"**Model**: {self.settings.anthropic_model}\n"
            f"**Duration**: {(finished_at - started_at).total_seconds():.1f}s\n"
            f"**Tokens**: {token_usage['input_tokens']} in / {token_usage['output_tokens']} out\n\n"
            f"---\n\n"
        )
        report_md = header + report_md

        report_path = self._save_report(routine.name, finished_at, report_md)

        return BriefingResult(
            routine=routine.name,
            started_at=started_at,
            finished_at=finished_at,
            report_md=report_md,
            report_path=report_path,
            token_usage=token_usage,
            success=True,
        )

    def _inject_context(self, prompt: str, routine: RoutineConfig) -> str:
        """Replace {{TODAY}} and {{ROUTINE}} placeholders."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return prompt.replace("{{TODAY}}", today).replace("{{ROUTINE}}", routine.name)

    def _save_report(self, routine_name: str, ts: datetime, content: str) -> Path:
        self.settings.reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{ts.strftime('%Y-%m-%d_%H%M')}_{routine_name}.md"
        path = self.settings.reports_dir / filename
        path.write_text(content, encoding="utf-8")
        logger.info(f"Report saved: {path}")
        return path


# ---------------------------------------------------------------------------
# Telegram notifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        if not self.enabled:
            logger.warning("Telegram not configured — notifications disabled")

    async def send_briefing_notification(self, result: BriefingResult, routine: RoutineConfig) -> None:
        if not self.enabled or not routine.telegram_summary:
            return

        if not result.success:
            text = (
                f"❌ [TRADING-ROUTINES] {result.routine}\n\n"
                f"Routine failed: {result.error}\n"
                f"Timestamp: {result.finished_at.isoformat()}"
            )
        else:
            # Extract first lines of report as summary (skip metadata header)
            lines = result.report_md.split("\n")
            body_start = next(
                (i for i, l in enumerate(lines) if l.startswith("---")), 0
            ) + 2
            summary = "\n".join(lines[body_start : body_start + 20])
            if len(summary) > routine.summary_max_chars:
                summary = summary[: routine.summary_max_chars] + "…"

            text = (
                f"📊 [TRADING-ROUTINES] {result.routine}\n\n"
                f"{summary}\n\n"
                f"📄 Full report: {result.report_path.name}\n"
                f"🕐 {result.finished_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                await client.post(
                    url,
                    json={
                        "chat_id": self.settings.telegram_chat_id,
                        "text": text[:4000],  # Telegram hard limit
                        "disable_web_page_preview": True,
                    },
                )
                logger.info("Telegram notification sent")
            except Exception as e:
                logger.error(f"Telegram send failed: {e}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class RoutineOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.briefer = Briefer(settings)
        self.notifier = TelegramNotifier(settings)
        self.routines = self._load_routines()

    def _load_routines(self) -> dict[str, RoutineConfig]:
        if not self.settings.config_file.exists():
            raise FileNotFoundError(f"Config file not found: {self.settings.config_file}")
        data = yaml.safe_load(self.settings.config_file.read_text(encoding="utf-8"))
        routines = {}
        for r in data.get("routines", []):
            routine = RoutineConfig(**r)
            routines[routine.name] = routine
        logger.info(f"Loaded {len(routines)} routine(s): {list(routines.keys())}")
        return routines

    async def run(self, routine_name: str) -> BriefingResult:
        if routine_name not in self.routines:
            raise KeyError(f"Unknown routine: {routine_name}. Available: {list(self.routines)}")
        routine = self.routines[routine_name]
        if not routine.enabled:
            logger.warning(f"Routine {routine_name} is disabled — running anyway (manual trigger)")

        # Run the sync briefer in a thread so we don't block the event loop
        result = await asyncio.to_thread(self.briefer.run_routine, routine)
        await self.notifier.send_briefing_notification(result, routine)
        return result

    def list_routines(self) -> Iterable[RoutineConfig]:
        return self.routines.values()

    async def schedule_forever(self) -> None:
        """Start APScheduler and block forever."""
        scheduler = AsyncIOScheduler(timezone=self.settings.timezone)

        for routine in self.routines.values():
            if not routine.enabled:
                logger.info(f"Skipping disabled routine: {routine.name}")
                continue
            trigger = CronTrigger.from_crontab(routine.cron, timezone=self.settings.timezone)
            scheduler.add_job(
                self.run,
                trigger=trigger,
                args=[routine.name],
                id=routine.name,
                name=f"routine:{routine.name}",
                misfire_grace_time=3600,  # allow 1h grace on missed runs
            )
            logger.info(f"Scheduled routine '{routine.name}' with cron: {routine.cron}")

        scheduler.start()
        logger.info("Scheduler started. Waiting for triggers... (Ctrl-C to stop)")

        # Block forever
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutdown requested")
            scheduler.shutdown()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_usage() -> None:
    print(__doc__)


async def _main_async(argv: list[str]) -> int:
    if len(argv) < 2:
        _print_usage()
        return 1

    settings = Settings()
    orchestrator = RoutineOrchestrator(settings)

    command = argv[1]

    if command == "run":
        if len(argv) < 3:
            print("Usage: run <routine_name>")
            print(f"Available: {[r.name for r in orchestrator.list_routines()]}")
            return 1
        routine_name = argv[2]
        result = await orchestrator.run(routine_name)
        if result.success:
            print(f"\n✅ Report saved: {result.report_path}")
            print(f"Tokens: {result.token_usage}")
            return 0
        else:
            print(f"\n❌ Routine failed: {result.error}")
            return 1

    elif command == "schedule":
        await orchestrator.schedule_forever()
        return 0

    elif command == "list":
        print("Configured routines:")
        for r in orchestrator.list_routines():
            status = "✅" if r.enabled else "⏸️ "
            print(f"  {status} {r.name:12}  cron: {r.cron}  prompt: {r.prompt_file}")
        return 0

    else:
        _print_usage()
        return 1


def main() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    exit_code = asyncio.run(_main_async(sys.argv))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
