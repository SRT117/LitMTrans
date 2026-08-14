"""Create the update manifest consumed by installed Windows builds."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION, GITHUB_REPOSITORY  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    if args.tag != f"v{APP_VERSION}":
        raise SystemExit(f"Tag {args.tag} does not match v{APP_VERSION}")
    digest = hashlib.sha256()
    with args.installer.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    manifest = {
        "version": APP_VERSION,
        "notes": args.notes.strip(),
        "installer": {
            "url": f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{args.tag}/{args.installer.name}",
            "sha256": digest.hexdigest(),
            "size": args.installer.stat().st_size,
        },
    }
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
