"""Filesystem locations that work both from source and from the packaged .exe.

Installed builds keep read-only program files next to ``RecapUpgrade.exe`` and
write user data (config.json, presets, jobs, logs, models) under
``%LOCALAPPDATA%\\RecapUpgrade``.  That makes an installer in Program Files
safe, keeps user data through upgrades, and avoids leaving secrets or job
output in the application folder.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "RecapUpgrade"
APP_VERSION = "2.6.3"


def is_frozen() -> bool:
    """True when running from the PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Read-only application folder (next to RecapUpgrade.exe when frozen)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_root() -> Path:
    """Read-only folder containing PyInstaller data files."""
    return Path(getattr(sys, "_MEIPASS", app_root()))


def data_root() -> Path:
    """Writable user-data folder.

    Source runs use the repository root (the existing behaviour).  Frozen
    builds use %LOCALAPPDATA%\\RecapUpgrade unless RECAP_DATA_DIR or a
    ``portable.flag`` file next to the exe asks for a portable layout.
    """
    if not is_frozen():
        root = app_root()
    else:
        override = os.environ.get("RECAP_DATA_DIR", "").strip()
        if override:
            root = Path(override).expanduser()
        elif (app_root() / "portable.flag").is_file():
            root = app_root() / "userdata"
        else:
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            root = Path(base) / APP_NAME if base else app_root() / "userdata"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_user_files() -> Path:
    """Create writable folders, migrate old portable data, seed .env."""
    root = data_root()
    for name in ("jobs", "logs", "models"):
        (root / name).mkdir(parents=True, exist_ok=True)

    # Upgrading from a portable build: keep the settings the user already had.
    if is_frozen() and root.resolve() != app_root().resolve():
        for name in ("config.json", "presets.json", ".env"):
            source = app_root() / name
            target = root / name
            if source.is_file() and not target.exists():
                try:
                    shutil.copy2(source, target)
                except OSError:
                    pass

    env_file = root / ".env"
    example = resource_path(".env.example")
    if not env_file.exists() and example.is_file():
        try:
            env_file.write_bytes(example.read_bytes())
        except OSError:
            pass
    return root


def resource_path(*parts: str) -> Path:
    """Find a bundled resource, preferring the PyInstaller extraction folder.

    Packaged onedir builds keep large external data (ffmpeg, recap_runtime,
    services) next to the exe, which is also accepted here as a fallback.
    """
    relative = Path(*parts)
    for base in (bundle_root(), app_root()):
        candidate = base / relative
        if candidate.exists():
            return candidate
    return app_root() / relative
