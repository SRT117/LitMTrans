"""Static release checks that do not require a packaged application."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import APP_AUTHOR, APP_VERSION, GITHUB_REPOSITORY  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION):
        fail("APP_VERSION must use MAJOR.MINOR.PATCH")
    if args.tag and args.tag != f"v{APP_VERSION}":
        fail(f"Tag {args.tag} does not match v{APP_VERSION}")
    if APP_AUTHOR != "SRT117" or GITHUB_REPOSITORY != "SRT117/LitMTrans":
        fail("Public author or repository identity changed")
    required = [
        "README.md", "PRIVACY.md", "SECURITY.md", "LICENSE", "CHANGELOG.md",
        "installer/LitMTrans.iss", "resources/icon.ico", "resources/assets/checkmark.svg",
        "resources/filters/export_fidelity.lua", "resources/fonts/SourceHanSerifCN-Regular.ttf",
        "resources/templates/reference.docx", "update.json",
    ]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        fail(f"Missing release files: {', '.join(missing)}")
    tracked = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    present = [name for name in tracked if (ROOT / name).exists()]
    forbidden = [name for name in present if name in {"AGENTS.md", "key.txt"} or name.startswith(("dist/", "build/", "output/", "tmp/"))]
    if forbidden:
        fail(f"Files must not be published: {', '.join(forbidden)}")
    print(f"Release metadata is valid for LitMTrans {APP_VERSION}.")


if __name__ == "__main__":
    main()
