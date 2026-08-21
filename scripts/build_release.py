"""Prepare version metadata and build the Windows PyInstaller directory."""

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import APP_AUTHOR, APP_NAME, APP_VERSION  # noqa: E402


def numeric_version() -> tuple[int, int, int, int]:
    parts = [int(part) for part in APP_VERSION.split(".")]
    if len(parts) != 3:
        raise SystemExit("APP_VERSION must use MAJOR.MINOR.PATCH")
    return (*parts, 0)


def assert_release_runtime_present(resources_dir: Path, stage: str = "Source") -> None:
    check_dir = resources_dir
    if not (check_dir / "pandoc.exe").exists() and (check_dir.parent / "_internal" / "resources" / "pandoc.exe").exists():
        check_dir = check_dir.parent / "_internal" / "resources"
    required = [
        check_dir / "pandoc.exe",
        check_dir / "mtranserver" / "bin" / "mtranserver-windows-amd64.exe",
        check_dir / "mtranserver" / "models" / "en_zh-Hans" / "model.enzh.intgemm.alphas.bin",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit(f"[{stage} check failed] Missing required release runtime components: {', '.join(missing)}")


def main() -> None:
    build_dir = ROOT / "build"
    dist_dir = ROOT / "dist"
    resources_dir = ROOT / "resources"
    assert_release_runtime_present(resources_dir, stage="Pre-build")

    build_dir.mkdir(exist_ok=True)
    if (dist_dir / APP_NAME).exists():
        shutil.rmtree(dist_dir / APP_NAME)
    version = numeric_version()
    version_text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={version}, prodvers={version}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{APP_AUTHOR}'),
      StringStruct('FileDescription', '{APP_NAME} Windows desktop application'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{APP_VERSION}')
    ])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
"""
    (build_dir / "version_info.txt").write_text(version_text, encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "litmtrans.spec"],
        cwd=ROOT,
        check=True,
    )
    assert_release_runtime_present(dist_dir / APP_NAME / "resources", stage="Post-build output")
    print(f"Build completed successfully with full runtime bundled for {APP_NAME} v{APP_VERSION}.")


if __name__ == "__main__":
    main()

