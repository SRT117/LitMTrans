"""Download the fixed Windows runtime used by release builds."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_URL = (
    "https://github.com/SRT117/LitMTrans/releases/download/"
    "runtime-windows-v1/LitMTrans-runtime-windows-v1.zip"
)
RUNTIME_SHA256 = "fc2c732573717db29e406c5164bd5687b1f7d1cb7908e6d4a49051736c68e406"


def main() -> None:
    resources = ROOT / "resources"
    with tempfile.TemporaryDirectory() as temporary_directory:
        archive = Path(temporary_directory) / "runtime.zip"
        print("Downloading Windows release runtime...")
        urllib.request.urlretrieve(RUNTIME_URL, archive)

        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != RUNTIME_SHA256:
            raise SystemExit("Release runtime download did not match its expected SHA-256.")

        with zipfile.ZipFile(archive) as package:
            package.extractall(resources)

    required = [
        resources / "pandoc.exe",
        resources / "mtranserver" / "bin" / "mtranserver-windows-amd64.exe",
        resources / "mtranserver" / "config",
        resources / "mtranserver" / "models" / "en_zh-Hans",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"Release runtime is incomplete: {', '.join(missing)}")
    print("Windows release runtime is ready.")


if __name__ == "__main__":
    main()
