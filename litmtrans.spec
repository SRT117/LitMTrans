# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules


project_root = Path.cwd()
main_script = project_root / "litmtrans.py"
resources = project_root / "resources"
# Default to bundling full runtime (Pandoc, MTranServer, models) if present on disk,
# unless LITMTRANS_BUNDLE_OPTIONAL_RUNTIME is explicitly set to "0".
env_flag = os.environ.get("LITMTRANS_BUNDLE_OPTIONAL_RUNTIME")
if env_flag is not None:
    bundle_optional_runtime = env_flag == "1"
else:
    bundle_optional_runtime = (resources / "pandoc.exe").exists() and (resources / "mtranserver" / "bin").exists()

# The default local-translation release contains only English → Simplified
# Chinese.  The UI derives its selectable language directions from the model
# directories actually included with the application.
MTRAN_RELEASE_MODELS = ("en_zh-Hans",)

# Release builds include Pandoc and the MTranServer runtime so users do not
# need to install them separately. Every bundled third-party component must
# have its license and redistribution terms recorded before release.
datas = []
for relative_path, destination in (
    ("assets", "resources/assets"),
    ("filters", "resources/filters"),
    ("fonts", "resources/fonts"),
    ("templates", "resources/templates"),
    ("icon.ico", "resources"),
    ("README.md", "resources"),
    ("指南.pdf", "resources"),
):
    source = resources / relative_path
    if source.exists():
        datas.append((str(source), destination))
for document_name in ("LICENSE", "PRIVACY.md", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"):
    datas.append((str(project_root / document_name), "docs"))

# Pandoc 与 MTranServer 全量离线机翻运行时（含模型，开箱即用）
if bundle_optional_runtime:
    pandoc_exe = resources / "pandoc.exe"
    if pandoc_exe.exists():
        datas.append((str(pandoc_exe), "resources"))
        pandoc_lic = project_root / "licenses" / "pandoc"
        if pandoc_lic.exists():
            datas.append((str(pandoc_lic), "licenses/pandoc"))

    mtran_bin = resources / "mtranserver" / "bin"
    if mtran_bin.exists():
        for relative_path, destination in (
            ("mtranserver/bin", "resources/mtranserver/bin"),
            ("mtranserver/config", "resources/mtranserver/config"),
            ("mtranserver/README.md", "resources/mtranserver"),
        ):
            source = resources / relative_path
            if source.exists():
                datas.append((str(source), destination))
        mtran_lic = project_root / "licenses" / "mtranserver"
        if mtran_lic.exists():
            datas.append((str(mtran_lic), "licenses/mtranserver"))

        for model_name in MTRAN_RELEASE_MODELS:
            model_dir = resources / "mtranserver" / "models" / model_name
            if not model_dir.is_dir():
                raise SystemExit(f"Missing selected MTranServer language pack: {model_name}")
            datas.append((str(model_dir), f"resources/mtranserver/models/{model_name}"))

binaries = []

hiddenimports = [
    "layout_translate_preview",
    "html.parser",
    "html.entities",
    "mimetypes",
    "xml.etree.ElementTree",
    "urllib.parse",
    "urllib.request",
]
hiddenimports += collect_submodules("docx")


a = Analysis(
    [str(main_script)],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LitMTrans",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(resources / "icon.ico") if (resources / "icon.ico").is_file() else None,
    version=str(project_root / "build" / "version_info.txt") if (project_root / "build" / "version_info.txt").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LitMTrans",
)
