"""RecapUpgrade desktop entry point and packaged-worker dispatcher.

Source runs:
    py -3.12 app/recap_upgrade.py

PyInstaller runs:
    RecapUpgrade.exe
    RecapUpgrade.exe --run-worker render_worker.py <job-config.json>
    RecapUpgrade.exe --self-test
    RecapUpgrade.exe --smoke-test

A frozen build cannot launch ``python render_worker.py`` because the .py files
live inside the bundle.  The GUI therefore relaunches the same exe with
``--run-worker`` and this module routes that call to the real worker ``main``.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _run_worker(script_name: str, cfg_path: str) -> int:
    """Run planner_worker.py / render_worker.py inside this frozen process."""
    module_name = Path(script_name).stem
    module = importlib.import_module(module_name)
    sys.argv = [script_name, cfg_path]
    return int(module.main())


def _hide_console() -> None:
    """A console is needed for worker pipes; hide it for the normal GUI."""
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _self_test() -> int:
    """Verify that every runtime dependency and external file is present."""
    from app_paths import (APP_NAME, APP_VERSION, app_root, data_root, ensure_user_files,
                           is_frozen, resource_path)

    ensure_user_files()
    failures: list[str] = []
    print(f"app={APP_NAME} {APP_VERSION}")
    print(f"frozen={is_frozen()}")
    print(f"app_root={app_root()}")
    print(f"data_root={data_root()}")

    modules = [
        "tkinter", "tkinterdnd2", "PIL", "numpy", "edge_tts", "av",
        "faster_whisper", "ctranslate2", "tokenizers", "onnxruntime",
        "playwright.sync_api", "google.genai", "fontTools", "dotenv",
    ]
    for name in modules:
        try:
            __import__(name)
            print(f"OK import {name}")
        except Exception as exc:                       # noqa: BLE001
            failures.append(f"import {name}: {exc}")
            print(f"FAIL import {name}: {exc}")

    for relative in ("factory_presets.json", "recap_runtime/services/whisper_service.py",
                     "recap_runtime/services/ai_service.py", "services/text_overlay_service.py"):
        path = resource_path(*relative.split("/"))
        if path.is_file():
            print(f"OK file {relative}")
        else:
            failures.append(f"missing {relative}")
            print(f"FAIL file {relative}")

    try:
        from controller import _tool_path, load_config
        cfg = load_config()
        for tool in ("ffmpeg", "ffprobe"):
            path = Path(_tool_path(cfg, tool))
            if path.is_file():
                print(f"OK {tool} {path}")
            else:
                failures.append(f"{tool}.exe not found")
                print(f"FAIL {tool}.exe not found")
    except Exception as exc:                           # noqa: BLE001
        failures.append(f"controller load: {exc}")
        print(f"FAIL controller load: {exc}")

    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nSELF-TEST OK")
    return 0


def _smoke_test() -> int:
    """Start Tk, build the real window, then close it (packaging check)."""
    from controller import App

    app = App()
    app.update_idletasks()
    app.update()
    print(f"pitch={app.edge_pitch_hz.get():+d}Hz")
    print(f"presets={len(app.preset_manager.get_preset_names())}")
    app.destroy()
    print("SMOKE TEST OK")
    return 0


def _render_test() -> int:
    """Render subtitle + title cards (Myanmar and English) with the bundled Chromium path."""
    import sys

    from app_paths import app_root, data_root

    root = app_root()
    if str(root) not in sys.path:          # source run: services\ lives at the repo root
        sys.path.insert(0, str(root))
    folder = data_root() / "logs" / "render-test"
    folder.mkdir(parents=True, exist_ok=True)

    from services.text_overlay_service import find_latin_font, make_video_title_png
    from subtitle_png import sample

    subtitle = sample({"font": "Myanmar Text"}, (640, 360), folder)
    title = make_video_title_png("RecapUpgrade", folder / "title.png", {"font_size": 40, "panel_width": 640})
    english_subtitle = sample({"font": "Segoe UI"}, (640, 360), folder, "en")
    english_title = make_video_title_png(
        "RecapUpgrade English", folder / "title_en.png",
        {"font_size": 40, "panel_width": 640, "font_path": str(find_latin_font()), "use_font_path": True})
    print(f"subtitle={subtitle} exists={subtitle.is_file()}")
    print(f"title={title} exists={title.is_file()}")
    print(f"english_subtitle={english_subtitle} exists={english_subtitle.is_file()}")
    print(f"english_title={english_title} exists={english_title.is_file()}")
    if all(p.is_file() for p in (subtitle, title, english_subtitle, english_title)):
        print("RENDER TEST OK")
        return 0
    print("RENDER TEST FAILED")
    return 1


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "--run-worker":
        return _run_worker(argv[2], argv[3])
    if "--self-test" in argv:
        return _self_test()
    if "--smoke-test" in argv:
        return _smoke_test()
    if "--render-test" in argv:
        return _render_test()

    from app_paths import is_frozen
    if is_frozen():
        _hide_console()

    from controller import App
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
