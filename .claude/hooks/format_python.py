"""PostToolUse hook: auto-format and lint-fix any Python file Claude Code edits.

Reads the hook payload from stdin. Works on Windows, macOS and Linux.
Skips quietly until Part 00 has created pyproject.toml.
Exit code 2 sends remaining lint errors back to Claude so it fixes them.
"""

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    if not file_path.endswith(".py"):
        return 0

    root = Path(payload.get("cwd") or ".")
    if not (root / "pyproject.toml").exists():
        return 0

    subprocess.run(["uv", "run", "ruff", "format", "--quiet", file_path], cwd=root, check=False)
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", "--quiet", file_path],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(f"ruff found issues in {file_path}:\n{result.stdout}{result.stderr}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
