"""CLI wrapper to pre-download the MMS Myanmar TTS model (spec §7.6).

Usage:
    py -3.12 app/download_mms.py [target_dir]

Requires transformers + torch + scipy (the optional MMS stack). If those are not
installed the script prints guidance and exits non-zero; the parent install step
tolerates that so the rest of the setup still succeeds.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make ``app`` importable when run as ``py app/download_mms.py`` from the project root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from app_paths import data_root  # noqa: E402
from tts_engine import download_mms_model  # noqa: E402


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else (data_root() / "models" / "mms-mya")
    try:
        ok = download_mms_model(target)
    except ImportError:
        print("❌ MMS TTS needs transformers + torch + scipy.")
        print("   Install with: py -3.12 -m pip install transformers torch scipy")
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
