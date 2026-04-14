"""
trading_routines — Automated trading intelligence briefings.

Runs weekly/monthly/quarterly research routines via the Anthropic API with
web search enabled, adaptive thinking, prompt caching, and streaming output.
Saves markdown reports and notifies Telegram.

Designed to run as a long-lived service (APScheduler) or as a one-shot CLI:

    python trading_routines.py run weekly            # manual trigger (streams to stdout)
    python trading_routines.py run monthly
    python trading_routines.py run quarterly
    python trading_routines.py schedule              # start scheduler daemon
    python trading_routines.py list                  # list available routines
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml
from anthropic import Anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Environment-driven settings. Loaded from .env or real env vars."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-4-6", alias="ANTHROPIC_MODEL")
    max_tokens: int = Field(default=16000, alias="MAX_TOKENS")
    max_web_searches: int = Field(default=10, alias="MAX_WEB_SEARCHES")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    reports_dir: Path = Field(default=Path("reports"), alias="REPORTS_DIR")
    config_file: Path = Field(default=Path("config.yaml"), alias="CONFIG_FILE")
    prompts_dir: Path = Field(default=Path("prompts"), alias="PROMPTS_DIR")
    system_prompt_file: Path = Field(
        default=Path("prompts/system.md"), alias="SYSTEM_PROMPT_FILE"
    )

    timezone: str = Field(default="Europe/Paris", alias="TIMEZONE")


# ---------------------------------------------------------------------------
# Routine definitions (loaded from config.yaml)
# ---------------------------------------------------------------------------

class RoutineConfig(BaseModel):
    name: str                           # "weekly" | "monthly" | "quarterly"
    enabled: bool = True
    cron: str                           # APScheduler cron expression (5-field)
    prompt_file: str                    # relative to prompts_dir
    telegram_summary: bool = True       # send a summary to Telegram
    summary_max_chars: int = 1500
    # Claude API tuning — per-routine overrides
    model: str | None = None            # None → settings.anthropic_model
    thinking_enabled: bool = True       # enable adaptive thinking
    effort: str = "high"               # low | medium | high | max


@dataclass(slots=True)
class BriefingResult:
    routine: str
    started_at: datetime
    finished_at: datetime
    report_md: str
    report_path: Path
    token_usage: dict
    success: bool
    model_used: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# Briefer: calls Claude API with web_search, adaptive thinking, prompt caching
# ---------------------------------------------------------------------------

class Briefer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self._system_prompt: str | None = self._load_system_prompt()

    def _load_system_prompt(self) -> str | None:
        path = self.settings.system_prompt_file
        if path.exists():
            content = path.read_text(encoding="utf-8")
            logger.info(f"System prompt loaded: {path} ({len(content):,} chars)")
            return content
        logger.warning(f"System prompt not found: {path} — prompt caching disabled")
        return None

    def run_routine(
        self,
        routine: RoutineConfig,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> BriefingResult:
        """Synchronous streaming call with adaptive thinking and prompt caching."""
        started_at = datetime.now(UTC)
        model = routine.model or self.settings.anthropic_model
        logger.info(
            f"Starting routine: {routine.name} | model={model} | "
            f"effort={routine.effort} | thinking={routine.thinking_enabled}"
        )

        prompt_path = self.settings.prompts_dir / routine.prompt_file
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

        prompt = prompt_path.read_text(encoding="utf-8")
        prompt = self._inject_context(prompt, routine)

        # Build optional kwargs — omit keys that are not needed
        extra: dict = {}
        if self._system_prompt:
            extra["system"] = [
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if routine.thinking_enabled:
            extra["thinking"] = {"type": "adaptive"}

        try:
            text_parts: list[str] = []
            with self.client.messages.stream(
                model=model,
                max_tokens=self.settings.max_tokens,
                output_config={"effort": routine.effort},
                tools=[
                    {
                        "type": "web_search_20260209",
                        "name": "web_search",
                        "max_uses": self.settings.max_web_searches,
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
                **extra,
            ) as stream:
                for chunk in stream.text_stream:  # auto-skips thinking blocks
                    text_parts.append(chunk)
                    if on_text_chunk:
                        on_text_chunk(chunk)
                response = stream.get_final_message()

        except Exception as e:
            logger.exception(f"API call failed for routine {routine.name}")
            finished_at = datetime.now(UTC)
            return BriefingResult(
                routine=routine.name,
                started_at=started_at,
                finished_at=finished_at,
                report_md="",
                report_path=Path(),
                token_usage={},
                success=False,
                model_used=model,
                error=str(e),
            )

        report_md = "".join(text_parts).strip()
        finished_at = datetime.now(UTC)

        usage = response.usage
        token_usage = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }
        cache_hit = token_usage["cache_read_input_tokens"] > 0
        duration = (finished_at - started_at).total_seconds()

        logger.info(
            f"Routine {routine.name} complete — "
            f"{token_usage['input_tokens']:,} in / {token_usage['output_tokens']:,} out | "
            f"cache write={token_usage['cache_creation_input_tokens']:,} "
            f"read={token_usage['cache_read_input_tokens']:,} "
            f"({'HIT' if cache_hit else 'MISS'}) | {duration:.1f}s"
        )

        cache_stats = ""
        if token_usage["cache_creation_input_tokens"] or token_usage["cache_read_input_tokens"]:
            cache_stats = (
                f" | cache write: {token_usage['cache_creation_input_tokens']:,}"
                f" / read: {token_usage['cache_read_input_tokens']:,}"
            )

        header = (
            f"# {routine.name.capitalize()} Trading Briefing\n\n"
            f"**Generated**: {finished_at.isoformat()}\n"
            f"**Model**: {model}\n"
            f"**Effort**: {routine.effort}"
            + (" | **Thinking**: adaptive" if routine.thinking_enabled else "")
            + f"\n**Duration**: {duration:.1f}s\n"
            f"**Tokens**: {token_usage['input_tokens']:,} in "
            f"/ {token_usage['output_tokens']:,} out"
            + cache_stats
            + "\n\n---\n\n"
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
            model_used=model,
        )

    def _inject_context(self, prompt: str, routine: RoutineConfig) -> str:
        """Replace {{TODAY}} and {{ROUTINE}} placeholders."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        if not self.enabled:
            logger.warning("Telegram not configured — notifications disabled")

    async def send_briefing_notification(
        self, result: BriefingResult, routine: RoutineConfig
    ) -> None:
        if not self.enabled or not routine.telegram_summary:
            return

        if not result.success:
            text = (
                f"❌ [TRADING-ROUTINES] {result.routine}\n\n"
                f"Routine failed: {result.error}\n"
                f"Timestamp: {result.finished_at.isoformat()}"
            )
        else:
            lines = result.report_md.split("\n")
            body_start = next(
                (i for i, line in enumerate(lines) if line.startswith("---")), 0
            ) + 2
            summary = "\n".join(lines[body_start : body_start + 20])
            if len(summary) > routine.summary_max_chars:
                summary = summary[: routine.summary_max_chars] + "…"

            cache_note = ""
            if result.token_usage.get("cache_read_input_tokens", 0) > 0:
                cache_note = " 💾 cache hit"

            text = (
                f"📊 [TRADING-ROUTINES] {result.routine}\n\n"
                f"{summary}\n\n"
                f"📄 Full report: {result.report_path.name}\n"
                f"🕐 {result.finished_at.strftime('%Y-%m-%d %H:%M UTC')}"
                + cache_note
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
    def __init__(self, settings: Settings) -> None:
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

    async def run(
        self,
        routine_name: str,
        stream_callback: Callable[[str], None] | None = None,
    ) -> BriefingResult:
        if routine_name not in self.routines:
            raise KeyError(f"Unknown routine: {routine_name}. Available: {list(self.routines)}")
        routine = self.routines[routine_name]
        if not routine.enabled:
            logger.warning(f"Routine {routine_name} is disabled — running anyway (manual trigger)")

        result = await asyncio.to_thread(self.briefer.run_routine, routine, stream_callback)
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
                misfire_grace_time=3600,
            )
            logger.info(f"Scheduled routine '{routine.name}' with cron: {routine.cron}")

        scheduler.start()
        logger.info("Scheduler started. Waiting for triggers... (Ctrl-C to stop)")

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

        def on_chunk(text: str) -> None:
            print(text, end="", flush=True)

        result = await orchestrator.run(routine_name, stream_callback=on_chunk)
        if result.success:
            print(f"\n\n✅ Report saved: {result.report_path}")
            print(f"Tokens: {result.token_usage}")
            return 0
        print(f"\n❌ Routine failed: {result.error}")
        return 1

    if command == "schedule":
        await orchestrator.schedule_forever()
        return 0

    if command == "list":
        print("Configured routines:")
        for r in orchestrator.list_routines():
            status = "✅" if r.enabled else "⏸️ "
            model_label = r.model or settings.anthropic_model
            print(
                f"  {status} {r.name:12}  cron: {r.cron:20}  "
                f"effort: {r.effort:6}  thinking: {r.thinking_enabled}  "
                f"model: {model_label}  prompt: {r.prompt_file}"
            )
        return 0

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
