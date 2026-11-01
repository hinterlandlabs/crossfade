"""Regenerate docs/reference/cli.md from the CLI's own --help output.  uv run python scripts/gen_cli_reference.py"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ["experiment", "scenario", "world", "traces"]
HEAD = ("# CLI reference\n\nGenerated from `crossfade --help` by `scripts/gen_cli_reference.py`. Every command accepts `--json` where it "
        "prints data. Exit codes: 0 success, 1 usage or validation error, 2 experiment not accepted, 3 integrity failure.\n")


def help_text(words: list[str]) -> str:
    env = {**os.environ, "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}
    raw = subprocess.run([str(Path(sys.executable).parent / "crossfade"), *words, "--help"], capture_output=True, text=True, env=env, check=True).stdout
    lines = []
    for line in raw.splitlines():
        if re.match(r"^\s*╰", line):
            continue
        m = re.match(r"^\s*╭─ (.*?) ─", line)
        line = f" {m.group(1)}" if m else line.replace("│", "").rstrip()
        if line.strip():
            lines.append(line.rstrip())
    return "\n".join(lines)


def commands(words: list[str]) -> list[str]:
    text = help_text(words)
    section = text.split(" Commands", 1)[1] if " Commands" in text else ""
    return [m.group(1) for m in re.finditer(r"^ (\w[\w-]*)(?: {2,}|$)", section, re.M)]


def main() -> None:
    out = [HEAD, "## crossfade\n\n```\n" + help_text([]) + "\n```\n"]
    for cmd in commands([]):
        subs = [[cmd, s] for s in commands([cmd])] if cmd in GROUPS else [[cmd]]
        for words in subs:
            out.append(f"## crossfade {' '.join(words)}\n\n```\n{help_text(words)}\n```\n")
    (ROOT / "docs" / "reference" / "cli.md").write_text("\n".join(out))


if __name__ == "__main__":
    main()
