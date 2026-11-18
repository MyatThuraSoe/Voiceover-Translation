# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the RecapUpgrade desktop app (onedir, Windows x64).

Build:
    py -3.12 -m PyInstaller --noconfirm --clean RecapUpgrade.spec

The spec bundles Python packages only.  Large, user-editable or frequently
updated runtime files (ffmpeg, recap_runtime, services, factory presets,
config.json) are copied next to RecapUpgrade.exe by build_exe.ps1 so they keep
working across rebuilds and can be replaced without rebuilding the exe.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()

datas = []
binaries = []
hiddenimports = [
    # Workers are launched by name from the packaged exe (--run-worker).
    "planner_worker",
    "render_worker",
    # Namespace package / dynamic imports.
    "services.text_overlay_service",
    "app_paths",
]

# Packages with native DLLs, data files, or dynamic imports that PyInstaller's
# static analysis cannot see on its own.
COLLECT_ALL = [
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "onnxruntime",
    "edge_tts",
    "playwright",
    "tkinterdnd2",
    "huggingface_hub",
    "fontTools",
]
for package in COLLECT_ALL:
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# google-genai ships a namespace package; collect it explicitly.
datas += collect_data_files("google.genai")
hiddenimports += collect_submodules("google.genai")
hiddenimports += collect_submodules("pydantic")

a = Analysis(
    [str(ROOT / "app" / "recap_upgrade.py")],
    pathex=[str(ROOT / "app"), str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Optional MMS stack: not bundled (users can install it separately).
        "torch", "transformers", "scipy",
        # Unrelated heavy packages.
        "matplotlib", "pandas", "IPython", "pytest", "PyQt5", "PySide6",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RecapUpgrade",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # worker stdout pipe; hidden immediately for the GUI
    icon=str(ROOT / "assets" / "RecapUpgrade.ico"),
    version=str(ROOT / "assets" / "version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RecapUpgrade",
)
