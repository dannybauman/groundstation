"""Did my release land? One line per way of having groundstation, then a verdict.

    uv run python -m groundstation.release_check [--installs PATH]

Every missing piece reads "not installed", never an error. --installs (or the
GROUNDSTATION_INSTALLS env var) is an Astrolabe brain's .dashboard-installs.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

MARKETPLACE_URL = "https://raw.githubusercontent.com/dannybauman/groundstation/main/.claude-plugin/marketplace.json"
_UPDATE = "claude plugin marketplace update groundstation && claude plugin update groundstation@groundstation"


def repo_versions(root: Path) -> dict[str, str]:
    """The three files whose versions must agree (evals/unit_checks.py asserts it)."""
    import tomllib

    return {
        "pyproject.toml": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "plugin.json": json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"],
        "marketplace.json": json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"],
    }


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def github_version() -> str | None:
    try:
        with urllib.request.urlopen(MARKETPLACE_URL, timeout=8) as r:
            return json.load(r)["plugins"][0]["version"]
    except Exception:
        return None


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def plugin_version(cdir: Path) -> str | None:
    try:
        return _json(cdir / "plugins" / "installed_plugins.json")["plugins"]["groundstation@groundstation"][0]["version"]
    except Exception:
        return None


def store_version(path: str | None) -> str | None:
    d = _json(Path(path)) if path else None
    try:
        return d.get("installed", d)["groundstation"]["version"]
    except Exception:
        return None


def skills_only(cdir: Path) -> bool:
    return (cdir / "skills" / "groundstation" / "SKILL.md").is_file() or (cdir / "skills" / "earth-data" / "SKILL.md").is_file()


def uv_tool_version() -> str | None:
    try:
        out = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"^groundstation v?(\S+)", out, re.M)
    return m.group(1) if m else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--installs", default=os.environ.get("GROUNDSTATION_INSTALLS"))
    args = ap.parse_args(argv)
    cdir = claude_dir()

    gh, cc, uvt = github_version(), plugin_version(cdir), uv_tool_version()
    rows = [("GitHub main", gh, "could not be reached"), ("Claude Code plugin", cc, "not installed")]
    store = None
    if args.installs:
        store = store_version(args.installs)
        rows.append(("Astrolabe Store copy", store, "not installed"))
    rows.append(("uv tool install", uvt, "not installed"))
    for name, v, none in rows:
        print(f"{name}: {'version ' + v if v else none}")
    if skills_only(cdir):
        print("Skills-only install: found (skills carry no version, so they cannot be compared)")

    have = {n: v for n, v, _ in rows[1:] if v}
    if not gh:
        print("CANT TELL: GitHub could not be reached")
    elif not have:
        print("NOTHING TO COMPARE: no versioned install found")
    elif all(v == gh for v in have.values()):
        print(f"MATCH: everything is on {gh}")
    else:
        stale = ", ".join(f"{n} {v}" for n, v in have.items() if v != gh)
        print(f"NO MATCH: GitHub has {gh}, behind: {stale}. Claude Code fix: {_UPDATE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
