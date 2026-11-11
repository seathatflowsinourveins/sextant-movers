"""Refuse a code brief whose "Upstream first" section is missing or empty (AGENTS.md Rule 1).

The section is read the way dangerfile.js reads a PR body section: a `## ` heading up to the
next one, HTML comments removed. Each field is one `- Name: value` line.
Usage: python3 -I scripts/brief_check.py BRIEF.md (or `-` for stdin); exit 1 names the gaps.
"""

import re
import sys

FIELDS = ("Shipped piece", "Verdict", "Where searched")
VERDICTS = ("use as is", "build on", "keep ours")


def missing(text: str) -> list[str]:
    match = re.search(r"^## Upstream first\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        return ['the "## Upstream first" section']
    section = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.S)
    gaps = []
    for field in FIELDS:
        value = re.search(rf"^- {field}:[ \t]*(\S.*)$", section, re.M)
        if not value:
            gaps.append(field)
        elif field == "Verdict" and not value.group(1).lower().startswith(VERDICTS):
            gaps.append(f"Verdict (one of: {', '.join(VERDICTS)})")
    return gaps


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "-"
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    gaps = missing(text)
    if gaps:
        print(f"brief_check: upstream first is not filled: {'; '.join(gaps)}", file=sys.stderr)
        sys.exit(1)
