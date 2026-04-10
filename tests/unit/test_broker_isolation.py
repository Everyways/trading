"""AST isolation test — §17.2.

Verifies that no module outside app/providers/alpaca/ imports from alpaca-py.
This enforces the broker isolation rule: only the provider package knows the SDK.
"""

from __future__ import annotations

import ast
import pathlib


def test_alpaca_not_imported_outside_provider() -> None:
    """Scan every .py file in app/ and assert none outside the alpaca provider
    package directly imports from the alpaca SDK."""
    root = pathlib.Path("app")
    allowed = root / "providers" / "alpaca"

    violations: list[str] = []

    for py_file in sorted(root.rglob("*.py")):
        if py_file.is_relative_to(allowed):
            continue  # alpaca provider itself is allowed to import alpaca

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # skip files with syntax errors (shouldn't happen)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (alias.name or "").startswith("alpaca"):
                        violations.append(
                            f"{py_file}:{node.lineno}: 'import {alias.name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("alpaca"):
                    violations.append(
                        f"{py_file}:{node.lineno}: 'from {module} import ...'"
                    )

    assert not violations, (
        "Broker isolation violated — alpaca imports found outside app/providers/alpaca/:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
