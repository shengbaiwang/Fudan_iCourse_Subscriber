#!/usr/bin/env python3
"""Launch a command with only the model secrets selected by validated config.

GitHub Actions cannot dynamically address a Secret by a name stored in a
Variable, so the workflow passes its secrets as JSON to this short-lived
launcher. The config validator limits selectable names to legacy model keys
or ``LLM_*_API_KEY``/``LLM_*_BASE_URL``. ``SECRETS_CONTEXT`` is removed from
the child environment before execution.

Usage: ``python scripts/provider_env.py -- python -u main.py``.

With no command it retains the old behaviour and prints shell-safe exports,
which is useful for diagnostics and backwards compatibility.
"""

from __future__ import annotations

import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_context() -> dict[str, object]:
    try:
        context = json.loads(os.environ.get("SECRETS_CONTEXT") or "{}")
    except ValueError as exc:
        raise ValueError("SECRETS_CONTEXT is not valid JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("SECRETS_CONTEXT must be a JSON object")
    # GitHub Secret names are case-insensitive; normalize for lookup only.
    return {str(key).upper(): value for key, value in context.items() if value}


def main() -> int:
    try:
        lookup = _load_context()
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    try:
        from src.runtime.config import MODEL_PROVIDERS
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    selected: dict[str, str] = {}
    for provider in MODEL_PROVIDERS:
        for field in ("api_key_env", "base_url_env"):
            name = provider.get(field)
            if not name or name in selected:
                continue
            value = lookup.get(name.upper())
            if value:
                selected[name] = str(value)

    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if command:
        child_env = os.environ.copy()
        child_env.pop("SECRETS_CONTEXT", None)
        child_env.update(selected)
        os.execvpe(command[0], command, child_env)

    for name, value in selected.items():
        print(f"export {name}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
