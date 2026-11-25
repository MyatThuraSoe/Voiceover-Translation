from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:  # optional: drag-and-drop needs tkinterdnd2 (spec §9.1)
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_AVAILABLE = True
except Exception:                       # noqa: BLE001 — app must start without the package
    TkinterDnD, DND_FILES = None, None
    _DND_AVAILABLE = False

_AppBase = TkinterDnD.Tk if _DND_AVAILABLE else tk.Tk

from app_paths import APP_VERSION, app_root, data_root, ensure_user_files, is_frozen, resource_path
from core import (FRAME_FILLS, OUTPUT_RATIOS, QUALITY_LABELS, normalize_segments, output_canvas, probe_video_size,
                  video_quality)
from preview_tools import probe_duration as preview_probe_duration, extract_preview_frame, scale_logo_preview
from narration_editor import NarrationEditor, failed_segment_from_error
from splitter_tab import SplitterTab
from gemini_tts import GEMINI_VOICES
from recap_planner import SOURCE_LANGUAGES
from recap_subtitles import SubtitleControls
from subtitle_fonts import latin_fonts, myanmar_fonts
from subtitle_png import geometry as subtitle_geometry, sample as subtitle_sample
from preset_manager import PresetManager
from render_variants import (default_profile_for_ratio, final_output_filename, missing_artifacts,
                             prepare_second_ratio, profile_from_settings, ratio_key,
                             record_variant, restore_voice_config)
from batch_manager import BatchManager
from filter_chain import (ASPECT_LOCKS, MAX_CUSTOM_OVERLAYS, build_prefilter, format_time_seconds,
                          normalize_color, normalize_crop, normalize_overlay, overlay_visible_at,
                          parse_time_seconds)
from tts_engine import ENGINE_LABELS, PREVIEW_TEXT

ROOT_DIR = app_root()
DATA_DIR = ensure_user_files()
CONFIG_PATH = DATA_DIR / "config.json"
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
EVENT_PREFIX = "@@VRS_EVENT@@"

BG = "#0d0f14"
PANEL = "#151922"
FIELD = "#0a0c10"
BORDER = "#303744"
FG = "#e7eaf0"
MUTED = "#9da7b6"
ACCENT = "#5b8cff"
ACCENT_HOVER = "#739dff"
DANGER = "#ff6b6b"
SUCCESS = "#55d187"
PREVIEW_W = 640
PREVIEW_H = 360

EDGE_VOICES = ("my-MM-ThihaNeural", "my-MM-NilarNeural")

# English Edge TTS voices (2026-11): the narrator rewrites English wording instead of translating to Burmese.
# en-GB-RyanNeural is first: it is the requested default for English UK.
EDGE_VOICES_EN = (
    "en-GB-RyanNeural",
    "en-GB-SoniaNeural",
    "en-GB-LibbyNeural",
    "en-GB-MaisieNeural",
    "en-GB-ThomasNeural",
    "en-US-GuyNeural",
    "en-US-AriaNeural",
    "en-US-JennyNeural",
    "en-US-ChristopherNeural",
    "en-US-EricNeural",
    "en-US-MichelleNeural",
    "en-US-RogerNeural",
    "en-US-SteffanNeural",
    "en-AU-WilliamNeural",
    "en-AU-NatashaNeural",
    "en-CA-LiamNeural",
    "en-CA-ClaraNeural",
    "en-IE-ConnorNeural",
    "en-IE-EmilyNeural",
    "en-IN-PrabhatNeural",
    "en-IN-NeerjaNeural",
    "en-NZ-MitchellNeural",
    "en-NZ-MollyNeural",
    "en-ZA-LukeNeural",
    "en-ZA-LeahNeural",
    "en-HK-SamNeural",
    "en-HK-YanNeural",
    "en-KE-ChilembaNeural",
    "en-KE-AsiliaNeural",
    "en-NG-AbeoNeural",
    "en-NG-EzinneNeural",
    "en-PH-JamesNeural",
    "en-PH-RosaNeural",
    "en-SG-WayneNeural",
    "en-SG-LunaNeural",
    "en-TZ-ElimuNeural",
    "en-TZ-ImaniNeural",
)
NARRATOR_VOICES = EDGE_VOICES + GEMINI_VOICES        # gemini_tts.py: one narrator voice for the whole video
NARRATOR_VOICES_EN = EDGE_VOICES_EN + GEMINI_VOICES
TARGET_LABELS = {"mm": "Myanmar  ·  translate", "en": "English  ·  rewrite"}
FILL_LABELS = {"blur": "Blur background", "black": "Black bars", "crop": "Crop to fill"}
# Source language (2026-10-17): any language → Myanmar; Auto-detect asks Whisper which language the video speaks.
LANGUAGE_LABELS = {"auto": "Auto-detect", **SOURCE_LANGUAGES}
ENGLISH_PREVIEW_TEXT = ("This is a short sample of the English narrator. "
                        "Tonight, we follow a story of secrets, danger, and one impossible choice.")


def _latin_title_font() -> str:
    """A bold Latin font file for English title/extra-text overlays (Windows)."""
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "calibrib.ttf", "verdanab.ttf",
                 "tahomabd.ttf", "segoeui.ttf", "arial.ttf"):
        path = windir / name
        if path.is_file():
            return str(path)
    return ""

# V2 (spec §9.5): actionable hints appended to worker errors.
ERROR_SUGGESTIONS = {
    "Edge TTS returned no usable MP3": "Check your internet connection and try again — Edge TTS sometimes has transient failures.",
    "Edge TTS stopped at line": "Check your connection. The checkpoint keeps finished lines; press START / RESUME to continue.",
    "Gemini API key": "Check your .env file. Get a free key at aistudio.google.com/apikey",
    "ffmpeg not found": "Run INSTALL_REQUIREMENTS.bat to download FFmpeg automatically.",
    "ffprobe": "Run INSTALL_REQUIREMENTS.bat — ffprobe.exe belongs next to ffmpeg.exe.",
    "CUDA out of memory": "Close other GPU applications, or set WHISPER_DEVICE=cpu in config.json.",
    "No such file or directory": "A referenced file is missing. Check the logo / overlay / source paths in the settings.",
    "Production overlay render failed": "Check the logo and custom overlay images (valid PNG/JPG). Try disabling them to isolate the problem.",
    "All TTS engines failed": "No TTS engine could produce audio. Check the network, or switch the Engine back to Edge TTS.",
    "MMS Myanmar TTS needs": "Install the optional local voice: python -m pip install transformers torch scipy",
}

# One-click viral format buttons (spec §9.2): (label, settings dict).
VIRAL_FORMATS = (
    ("📱 9:16 Shorts", {"output_ratio": "9:16", "frame_fill": "blur", "video_quality": "1080",
                         "zoom_factor": 1.25, "mirror_video": True}),
    ("🖥️ 16:9 YouTube", {"output_ratio": "16:9", "frame_fill": "blur", "video_quality": "1080",
                          "zoom_factor": 1.0, "mirror_video": False}),
    ("⬜ 1:1 Square", {"output_ratio": "1:1", "frame_fill": "blur", "video_quality": "720",
                        "zoom_factor": 1.10, "mirror_video": False}),
    ("🎬 Cinematic", {"output_ratio": "16:9", "frame_fill": "crop", "video_quality": "1080",
                       "zoom_factor": 1.08, "mirror_video": True}),
)

ASPECT_OPTIONS = tuple(ASPECT_LOCKS.keys())


def _tool_path(cfg: dict, name: str) -> str:
    """ffmpeg / ffprobe: config.json path, the tool's own ffmpeg folder, PATH, then C:\\ffmpeg\\bin."""
    for candidate in (str(cfg.get(f"{name}_path") or ""), str(ROOT_DIR / "ffmpeg" / f"{name}.exe"),
                      str(ROOT_DIR / "ffmpeg" / "bin" / f"{name}.exe"), shutil.which(name) or "",
                      rf"C:\ffmpeg\bin\{name}.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    return name


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_edge_rate_percent(value: object) -> int:
    text = str(value or "+0%").strip()
    match = re.fullmatch(r"([+-]?\d{1,3})%", text)
    percent = int(match.group(1)) if match else 0
    return max(-50, min(percent, 100))


def _format_edge_rate(percent: object) -> str:
    value = max(-50, min(int(round(float(percent))), 100))
    return f"{value:+d}%"


def _parse_edge_pitch_hz(value: object) -> int:
    text = str(value or "+0Hz").strip()
    match = re.fullmatch(r"([+-]?\d{1,3})\s*Hz", text, re.IGNORECASE)
    hz = int(match.group(1)) if match else 0
    return max(-50, min(hz, 50))


def _format_edge_pitch(hz: object) -> str:
    value = max(-50, min(int(round(float(hz))), 50))
    return f"{value:+d}Hz"


def _safe_name(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:80] or "recap_test"


def _job_dir_for(source: Path) -> Path:
    raw = str(source.resolve()).encode("utf-8")
    sig = hashlib.sha1(raw).hexdigest()[:8]
    return JOBS_DIR / f"{_safe_name(source.stem)}_{sig}"


BATCH_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


def _natural_key(path: Path) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def _list_batch_videos(folder: Path) -> list[Path]:
    """Videos directly inside folder (not subfolders), in natural filename order."""
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in BATCH_VIDEO_EXTS),
        key=_natural_key,
    )


def _job_final_output(job_dir: Path) -> Path | None:
    final = job_dir / "edge_tts_smart_sync" / "final_edge_tts_smart_sync.mp4"
    return final if final.is_file() and final.stat().st_size > 10000 else None


def _native_crash_message(code: int) -> str:
    unsigned = code & 0xFFFFFFFF
    if code == -1073740791 or unsigned == 0xC0000409:
        return (
            "Child process native crash (0xC0000409).\n\n"
            "ဒီ native crash ထပ်ပေါ်ရင် jobs/<job>/run.log ကိုစစ်ပါ။"
        )
    return f"Child process closed with exit code {code}. run.log ကိုစစ်ပါ။"


class App(_AppBase):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"RecapUpgrade {APP_VERSION} - Myanmar Voiceover Studio")
        self.geometry("1580x900")
        self.minsize(1100, 660)
        try:
            self.state("zoomed")  # start maximised so all three panes get real space
        except tk.TclError:
            pass
        self.configure(bg=BG)
        self.cfg = load_config()
        self.msgq: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_proc: subprocess.Popen | None = None
        self.current_job: Path | None = None
        self.run_log_path: Path | None = None
        self.proc_lock = threading.Lock()

        self.gemini_env = tk.StringVar(value=self.cfg.get("gemini_env_path") or str(DATA_DIR / ".env"))
        self.source = tk.StringVar(value="")
        self.source_title = tk.StringVar(value="")
        saved_language = str(self.cfg.get("source_language") or "auto")
        self.source_language = tk.StringVar(value=LANGUAGE_LABELS.get(saved_language, LANGUAGE_LABELS["auto"]))
        self.final_folder = tk.StringVar(value=str(self.cfg.get("final_output_folder") or ""))
        saved_target = str(self.cfg.get("target_language") or "mm").strip().lower()
        if saved_target not in TARGET_LABELS:
            saved_target = "mm"
        self.target_language = tk.StringVar(value=TARGET_LABELS[saved_target])
        if saved_target == "en":
            saved_voice = str(self.cfg.get("edge_tts_voice_en") or self.cfg.get("narrator_voice") or EDGE_VOICES_EN[0])
            fallback_voice = EDGE_VOICES_EN[0]
            voices = NARRATOR_VOICES_EN
        else:
            saved_voice = str(self.cfg.get("edge_tts_voice_mm") or self.cfg.get("narrator_voice")
                              or self.cfg.get("edge_tts_voice") or EDGE_VOICES[0])
            fallback_voice = EDGE_VOICES[0]
            voices = NARRATOR_VOICES
        self.edge_voice = tk.StringVar(value=saved_voice if saved_voice in voices else fallback_voice)
        self.gemini_direction = tk.StringVar(value=str(self.cfg.get("gemini_direction") or ""))
        self._subtitle_preview_key = None
        self._subtitle_preview_path = None
        self._subtitle_busy = False
        self._subtitle_scaled_key = None
        self._subtitle_photo = None
        self.edge_rate_percent = tk.DoubleVar(value=float(_parse_edge_rate_percent(self.cfg.get("edge_tts_rate", "+0%"))))
        self.edge_pitch_hz = tk.IntVar(value=_parse_edge_pitch_hz(self.cfg.get("edge_tts_pitch", "+0Hz")))
        self.max_speed = tk.StringVar(value=str(self.cfg.get("max_video_speed", 1.25)))
        self.max_slow = tk.StringVar(value=str(self.cfg.get("max_video_slow", 1.35)))
        self.mirror_video = tk.BooleanVar(value=bool(self.cfg.get("mirror_video", True)))
        self.voice_volume = tk.IntVar(value=int(self.cfg.get("voice_volume_percent", 100) or 100))
        self.short_pauses = tk.BooleanVar(value=bool(self.cfg.get("short_pauses", True)))
        self.zoom_factor = tk.DoubleVar(value=float(self.cfg.get("zoom_factor", 1.05)))
        saved_ratio = str(self.cfg.get("output_ratio") or "16:9")
        self.output_ratio = tk.StringVar(value=saved_ratio if saved_ratio in OUTPUT_RATIOS else "16:9")
        self._active_ratio_key = ratio_key(self.output_ratio.get())
        self.export_two_ratios = tk.BooleanVar(value=bool(self.cfg.get("export_two_ratios", False)))
        saved_second = str(self.cfg.get("second_output_ratio") or "")
        if saved_second not in OUTPUT_RATIOS:
            saved_second = "16:9" if self.output_ratio.get() == "9:16" else "9:16"
        self.second_output_ratio = tk.StringVar(value=saved_second)
        self.editing_second_ratio = False
        self._main_ratio_before_style_edit: str | None = None
        self.frame_fill = tk.StringVar(value=FILL_LABELS.get(str(self.cfg.get("frame_fill") or "blur"), FILL_LABELS["blur"]))
        self.quality = tk.StringVar(value=QUALITY_LABELS[video_quality(self.cfg.get("video_quality"))])
        self.source_size = (0, 0)
        self.smooth_freeze = tk.BooleanVar(value=bool(self.cfg.get("smooth_freeze_fallback", False)))
        self.max_freeze_hold = tk.StringVar(value=str(self.cfg.get("max_freeze_hold", 0.75)))
        self.render_final = tk.BooleanVar(value=bool(self.cfg.get("render_final_video", True)))
        self.reuse_plan = tk.BooleanVar(value=bool(self.cfg.get("reuse_transcript_and_plan", self.cfg.get("reuse_whisper_and_plan", True))))
        self.status = tk.StringVar(value="READY")
        self.stage = tk.StringVar(value="Idle")
        self.job_display = tk.StringVar(value="Job output: not selected")
        # Folder batch: when non-empty, START processes these files one by one.
        self.batch_files: list[Path] = []
        self.batch_folder: Path | None = None
        self.batch_manager = BatchManager()
        self._active_batch_item = None
        self._progress_started_at = 0.0
        self._last_progress_key = None

        # ---- V2 settings (spec §3-§10) -------------------------------------
        self.crop_enabled = tk.BooleanVar(value=bool(self.cfg.get("crop", {}).get("enabled", False)))
        saved_crop = normalize_crop(self.cfg.get("crop"))
        self.crop_x = tk.DoubleVar(value=float(saved_crop["x_percent"]))
        self.crop_y = tk.DoubleVar(value=float(saved_crop["y_percent"]))
        self.crop_w = tk.DoubleVar(value=float(saved_crop["width_percent"]))
        self.crop_h = tk.DoubleVar(value=float(saved_crop["height_percent"]))
        self.crop_aspect = tk.StringVar(value=str(saved_crop["aspect_lock"]))
        self.crop_enabled.set(bool(saved_crop["enabled"]))

        saved_cc = normalize_color(self.cfg.get("color_correction"))
        self.color_enabled = tk.BooleanVar(value=bool(saved_cc["enabled"]))
        self.brightness = tk.DoubleVar(value=float(saved_cc["brightness"]))
        self.contrast = tk.DoubleVar(value=float(saved_cc["contrast"]))
        self.saturation = tk.DoubleVar(value=float(saved_cc["saturation"]))

        self.custom_overlays: list[dict] = [normalize_overlay(o, i + 1) for i, o in
                                             enumerate((self.cfg.get("custom_overlays") or [])[:MAX_CUSTOM_OVERLAYS])]
        self.selected_overlay: int | None = None
        self._overlay_photos: list = []
        self._overlay_photo_cache: dict = {}
        self._overlay_time_vars: list[tuple[tk.StringVar, tk.StringVar]] = []
        self._overlay_window_labels: list[ttk.Label] = []
        self.overlays_host: ttk.Frame | None = None

        duck = dict(self.cfg.get("audio_ducking") or {})
        self.ducking_enabled = tk.BooleanVar(value=bool(duck.get("enabled", False)))
        self.duck_volume_percent = tk.IntVar(value=int(duck.get("duck_volume_percent", 30) or 30))

        engine_code = str(self.cfg.get("tts_engine") or "edge_tts")
        if engine_code not in ENGINE_LABELS:
            engine_code = "edge_tts"
        self.tts_engine_label = tk.StringVar(value=ENGINE_LABELS[engine_code])
        self.edge_tts_ssml = tk.BooleanVar(value=bool(self.cfg.get("edge_tts_ssml", False)))
        self.engine_status = tk.StringVar(value="")
        self.preset_manager = PresetManager(
            DATA_DIR,
            log=lambda m: self.msgq.put(("log", str(m))),
            factory_path=resource_path("factory_presets.json"),
        )
        self.preset_name = tk.StringVar(value="")
        self._pending_preset: dict = {}
        self._crop_box: dict | None = None
        self._crop_drag: tuple | None = None

        # Minimal production editor state. V0.3 sync/mirror/freeze behavior remains hidden/fixed.
        self.title_text = tk.StringVar(value="")
        self.title_size = tk.IntVar(value=int(self.cfg.get("title_font_size", 54)))
        self.logo_path = tk.StringVar(value=str(self.cfg.get("logo_path", "")))
        self.logo_enabled = tk.BooleanVar(value=bool(self.cfg.get("logo_enabled", False)))
        self.logo_width_frac = tk.DoubleVar(value=float(self.cfg.get("logo_width_fraction", 0.16)))
        self.logo_opacity = tk.DoubleVar(value=float(self.cfg.get("logo_opacity", 0.65)))
        self.extra_text = tk.StringVar(value="")
        self.extra_text_size = tk.IntVar(value=int(self.cfg.get("extra_text_font_size", 42)))
        self.extra_text_opacity = tk.DoubleVar(value=float(self.cfg.get("extra_text_opacity", 0.65)))
        self.preview_time = tk.DoubleVar(value=float(self.cfg.get("preview_seek_seconds", 1.0)))
        self.preview_duration = 0.0
        self.blur_boxes: list[dict[str, float]] = []
        self.selected_blur: int | None = None
        self.title_pos = list(self.cfg.get("title_position", [0.50, 0.12]))
        self.logo_pos = list(self.cfg.get("logo_position", [0.80, 0.06]))
        self.extra_text_pos = list(self.cfg.get("extra_text_position", [0.50, 0.84]))
        self._preview_photo = None
        self._logo_photo = None
        self._draw_blur_mode = False
        self._draw_start: tuple[float, float] | None = None
        self._temp_rect = None
        self._drag_kind: str | None = None
        self._drag_index: int | None = None
        self._drag_start: tuple[float, float] | None = None
        self._drag_original = None

        self._apply_dark_theme()
        self._build()
        if _DND_AVAILABLE:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_file_drop)
            except Exception as exc:            # noqa: BLE001 — Tk DnD is optional
                self._log(f"ℹ️ Drag-and-drop unavailable: {exc}")
        default_name = self.preset_manager.get_default_name()
        if default_name:
            self._load_preset(default_name, quiet=True)
        # Per-ratio text sizes/positions win over the global default preset.
        self._apply_ratio_profile(self.output_ratio.get(), quiet=True)
        self._refresh_preset_combo()
        self._refresh_batch_view()
        self.after(150, self._pump)

    def _apply_dark_theme(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Status.TLabel", background=BG, foreground=SUCCESS, font=("Segoe UI Semibold", 10))
        style.configure("TLabelframe", background=BG, foreground=FG, bordercolor=BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=BG, foreground=FG, font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.map("TEntry", fieldbackground=[("disabled", PANEL)], foreground=[("disabled", MUTED)])
        style.configure("TCombobox", fieldbackground=FIELD, foreground=FG, background=PANEL, arrowcolor=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)], selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        style.configure("TButton", background="#252b36", foreground=FG, bordercolor=BORDER, padding=(10, 6))
        style.map("TButton", background=[("active", "#333b49"), ("disabled", PANEL)], foreground=[("disabled", "#657080")])
        style.configure("Compact.TButton", background="#252b36", foreground=FG, bordercolor=BORDER, padding=(6, 2))
        style.map("Compact.TButton", background=[("active", "#333b49"), ("disabled", PANEL)], foreground=[("disabled", "#657080")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT, padding=(12, 7), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#33445f")], foreground=[("disabled", "#8892a0")])
        style.configure("Danger.TButton", background="#4a252a", foreground="#ffd9dc", bordercolor="#6a3038", padding=(10, 6))
        style.map("Danger.TButton", background=[("active", "#653039")])
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT, bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG,
                        bordercolor=BORDER, rowheight=22)
        style.configure("Treeview.Heading", background="#252b36", foreground=FG, bordercolor=BORDER)
        style.map("Treeview", background=[("selected", "#2f4a78")], foreground=[("selected", "#ffffff")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 5), bordercolor=BORDER)
        style.map("TNotebook.Tab", background=[("selected", "#252b36")], foreground=[("selected", FG)])
        style.configure("Tools.TNotebook.Tab", font=("Segoe UI Semibold", 11), padding=(22, 7))
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build(self) -> None:
        # Layout V33: one-line header, compact source bar, then three resizable panes
        # (Settings+Logs | Preview | Tools). The preview follows its pane size and always
        # shows the whole 16:9 frame; the long settings list scrolls inside its own pane.
        # Tools as tabs (2026-10-17): Recap, Splitter; more tools get their own tab.
        self.tools_tabs = ttk.Notebook(self, style="Tools.TNotebook")
        self.tools_tabs.pack(fill="both", expand=True)
        recap_tab = ttk.Frame(self.tools_tabs)
        self.tools_tabs.add(recap_tab, text="Recap")
        self.splitter_tab = SplitterTab(self.tools_tabs, cfg=self.cfg, save=lambda: save_config(self.cfg),
                                        tool=lambda name: _tool_path(self.cfg, name), logs_dir=LOGS_DIR)
        self.tools_tabs.add(self.splitter_tab, text="Splitter")
        if self.cfg.get("last_tool_tab") == "splitter":
            self.tools_tabs.select(self.splitter_tab)
        outer = ttk.Frame(recap_tab, padding=(12, 8, 12, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tk.Label(header, text="Recap Production", bg=BG, fg=FG, font=("Segoe UI Semibold", 15)).pack(side="left")
        tk.Label(header, text="Any language → Myanmar / English  |  Narrator only  |  Whisper + Gemini  |  Edge TTS  |  Smart Sync",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(14, 0), pady=(5, 0))

        source_bar = ttk.Frame(outer)
        source_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        source_bar.columnconfigure(1, weight=3)
        source_bar.columnconfigure(4, weight=1)
        ttk.Label(source_bar, text="Source Video").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.source_entry = ttk.Entry(source_bar, textvariable=self.source)
        self.source_entry.grid(row=0, column=1, sticky="ew")
        self.source_entry.bind("<KeyRelease>", lambda _event: self._clear_batch())
        source_buttons = ttk.Frame(source_bar)
        source_buttons.grid(row=0, column=2, padx=(6, 18), sticky="w")
        ttk.Button(source_buttons, text="Browse", style="Compact.TButton", command=self._browse_source).pack(side="left")
        ttk.Button(source_buttons, text="Folder", style="Compact.TButton", command=self._browse_folder).pack(side="left", padx=(6, 0))
        ttk.Label(source_bar, text="Gemini .env").grid(row=0, column=3, sticky="w", padx=(0, 8))
        ttk.Entry(source_bar, textvariable=self.gemini_env).grid(row=0, column=4, sticky="ew")
        ttk.Button(source_bar, text="Browse", style="Compact.TButton", command=self._browse_env).grid(row=0, column=5, padx=(6, 0))
        ttk.Label(source_bar, text="Save final to").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(source_bar, textvariable=self.final_folder).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(source_bar, text="Browse", style="Compact.TButton", command=self._browse_final_folder).grid(
            row=1, column=2, padx=(6, 18), pady=(6, 0), sticky="w")
        ttk.Label(source_bar, text="ဗလာထားရင် job folder ထဲမှာပဲ။ ရွေးထားရင် ပြီးတဲ့ video ကို <name>_Myanmar.mp4 (သို့) <name>_English.mp4 နဲ့ ဒီ folder ထဲ ထည့်ပေးမယ်။",
                  style="Muted.TLabel").grid(row=1, column=3, columnspan=3, sticky="w", pady=(6, 0))

        main = ttk.PanedWindow(outer, orient="horizontal")
        main.grid(row=2, column=0, sticky="nsew")

        # ---------------- left: scrollable settings (top) + Run Logs (bottom)
        left = ttk.PanedWindow(main, orient="vertical")
        settings_host = ttk.LabelFrame(left, text="Translation / Voice Settings", padding=3)
        settings_host.rowconfigure(0, weight=1)
        settings_host.columnconfigure(0, weight=1)
        settings_canvas = tk.Canvas(settings_host, bg=BG, highlightthickness=0, height=340)
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scroll = ttk.Scrollbar(settings_host, orient="vertical", command=settings_canvas.yview)
        settings_scroll.grid(row=0, column=1, sticky="ns")
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        source_box = ttk.Frame(settings_canvas, padding=(6, 4, 8, 4))
        settings_window = settings_canvas.create_window((0, 0), window=source_box, anchor="nw")
        source_box.bind("<Configure>", lambda _e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.bind("<Configure>", lambda e: settings_canvas.itemconfigure(settings_window, width=e.width))
        self._bind_wheel(settings_canvas, settings_canvas)
        source_box.columnconfigure(1, weight=1)
        hint_wrap = 200

        def hint(row: int, text: str) -> ttk.Label:
            label = ttk.Label(source_box, text=text, style="Muted.TLabel", wraplength=hint_wrap, justify="left")
            label.grid(row=row, column=2, sticky="w", padx=(8, 0))
            return label

        # ---- Presets (spec §3.5) — always at the top of the settings panel ----
        preset_box = ttk.LabelFrame(source_box, text="Preset", padding=(6, 4))
        preset_box.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        preset_box.columnconfigure(0, weight=1)
        self.preset_combo = ttk.Combobox(preset_box, textvariable=self.preset_name, state="readonly",
                                         values=(), width=26)
        self.preset_combo.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        preset_btns = ttk.Frame(preset_box)
        preset_btns.grid(row=1, column=0, columnspan=3, sticky="ew")
        ttk.Button(preset_btns, text="💾 Save As...", style="Compact.TButton",
                   command=self._save_preset).pack(side="left")
        ttk.Button(preset_btns, text="🗑️ Delete", style="Compact.TButton",
                   command=self._delete_preset).pack(side="left", padx=(5, 0))
        ttk.Button(preset_btns, text="⭐ Set Default", style="Compact.TButton",
                   command=self._set_default_preset).pack(side="left", padx=(5, 0))

        ttk.Label(source_box, text="Source language").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Combobox(source_box, textvariable=self.source_language, state="readonly",
                     values=tuple(LANGUAGE_LABELS.values())).grid(row=1, column=1, sticky="ew", pady=3)
        hint(1, "ဗီဒီယိုထဲက ပြောတဲ့ ဘာသာ။ Auto-detect = Whisper က နားထောင်ပြီး သိအောင်လုပ်မယ်")
        ttk.Label(source_box, text="Output").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.target_combo = ttk.Combobox(source_box, textvariable=self.target_language, state="readonly",
                                         values=tuple(TARGET_LABELS.values()))
        self.target_combo.grid(row=2, column=1, sticky="ew", pady=3)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._target_changed())
        self.target_hint = hint(2, "Myanmar = translate  ·  English = rewrite the English wording, same meaning")
        self.target_hint.configure(text=self._target_hint_text())

        self.edge_voice_label = ttk.Label(source_box, text="Narrator Voice")
        self.edge_voice_label.grid(row=11, column=0, sticky="w", padx=(0, 8), pady=3)
        self.edge_voice_combo = ttk.Combobox(source_box, textvariable=self.edge_voice, state="readonly",
                                             values=self._narrator_voices())
        self.edge_voice_combo.grid(row=11, column=1, sticky="ew", pady=3)
        self.edge_voice_hint = hint(11, self._narrator_voice_hint())

        self.edge_speed_label = ttk.Label(source_box, text="Narrator Speed")
        self.edge_speed_label.grid(row=12, column=0, sticky="w", padx=(0, 8), pady=3)
        self.edge_speed_frame = ttk.Frame(source_box)
        self.edge_speed_frame.grid(row=12, column=1, sticky="ew", pady=3)
        self.edge_speed_frame.columnconfigure(0, weight=1)
        self.edge_speed_slider = ttk.Scale(
            self.edge_speed_frame, from_=-50, to=100, orient="horizontal",
            variable=self.edge_rate_percent, command=self._edge_rate_changed,
        )
        self.edge_speed_slider.grid(row=0, column=0, sticky="ew")
        self.edge_speed_value = ttk.Label(self.edge_speed_frame, text=_format_edge_rate(self.edge_rate_percent.get()), width=7)
        self.edge_speed_value.grid(row=0, column=1, padx=(8, 0))
        self.edge_speed_hint = hint(12, "-50% slower · 0% normal · +100% faster")

        ttk.Label(source_box, text="Gemini voice style").grid(row=13, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(source_box, textvariable=self.gemini_direction).grid(row=13, column=1, sticky="ew", pady=3)
        hint(13, "Gemini only, e.g. calm storyteller — the voice itself never changes")

        # ---- V2 TTS engine (spec §7.4 / §9.3) --------------------------------
        ttk.Label(source_box, text="Engine").grid(row=14, column=0, sticky="w", padx=(0, 8), pady=3)
        engine_combo = ttk.Combobox(source_box, textvariable=self.tts_engine_label, state="readonly",
                                    values=tuple(ENGINE_LABELS.values()))
        engine_combo.grid(row=14, column=1, sticky="ew", pady=3)
        engine_combo.bind("<<ComboboxSelected>>", lambda _e: self._engine_changed())
        hint(14, "Edge = free + reliable · MMS/Piper = local · ElevenLabs needs a free-tier key")
        ttk.Button(source_box, text="▶ Preview Voice", style="Compact.TButton",
                   command=self._preview_voice).grid(row=15, column=1, sticky="w", pady=3)
        ttk.Checkbutton(source_box, text="Natural pauses (SSML)",
                        variable=self.edge_tts_ssml).grid(row=15, column=0, sticky="w", pady=3)
        hint(15, "Myanmar sentence breaks → short pauses. Edge TTS only.")
        self.engine_status_label = ttk.Label(source_box, textvariable=self.engine_status,
                                             style="Muted.TLabel", wraplength=320, justify="left")
        self.engine_status_label.grid(row=16, column=0, columnspan=3, sticky="w", pady=(2, 4))

        ttk.Label(source_box, text="Narrator Pitch").grid(row=17, column=0, sticky="w", padx=(0, 8), pady=3)
        pitch_frame = ttk.Frame(source_box)
        pitch_frame.grid(row=17, column=1, sticky="ew", pady=3)
        pitch_frame.columnconfigure(0, weight=1)
        self.edge_pitch_value = ttk.Label(pitch_frame, text=_format_edge_pitch(self.edge_pitch_hz.get()), width=7)
        self.edge_pitch_slider = ttk.Scale(
            pitch_frame, from_=-50, to=50, orient="horizontal",
            variable=self.edge_pitch_hz, command=self._pitch_changed,
        )
        self.edge_pitch_slider.grid(row=0, column=0, sticky="ew")
        self.edge_pitch_value.grid(row=0, column=1, padx=(8, 0))
        hint(17, "-50Hz lower · +0Hz natural · +50Hz higher (Edge TTS only)")
        self._engine_changed()

        self._bind_wheel_recursive(source_box, settings_canvas)
        self._settings_canvas = settings_canvas

        log_panel = ttk.LabelFrame(left, text="Run Logs", padding=5)
        log_panel.rowconfigure(0, weight=1)
        log_panel.columnconfigure(0, weight=1)
        self.log_notebook = ttk.Notebook(log_panel)
        self.log_notebook.grid(row=0, column=0, sticky="nsew")
        self.logbox = self._create_log_tab("Main Log")
        left.add(settings_host, weight=3)
        left.add(log_panel, weight=2)

        # ---------------- center: preview that fills its pane
        preview_box = ttk.LabelFrame(main, text="Preview — drag Title / Logo / Subtitle, draw Blur box", padding=7)
        preview_box.columnconfigure(0, weight=1)
        preview_box.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(preview_box, width=PREVIEW_W, height=PREVIEW_H, bg="#05070a", highlightthickness=1, highlightbackground=BORDER, cursor="crosshair")
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self.preview_canvas.bind("<ButtonPress-1>", self._preview_press)
        self.preview_canvas.bind("<B1-Motion>", self._preview_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._preview_release)
        self.preview_canvas.bind("<Double-Button-1>", self._preview_double_click)
        self.preview_canvas.bind("<Configure>", self._preview_resized)
        self._preview_resize_job = None
        self._preview_canvas_size = (PREVIEW_W, PREVIEW_H)

        seek_row = ttk.Frame(preview_box)
        seek_row.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        seek_row.columnconfigure(1, weight=1)
        ttk.Button(seek_row, text="◀ 5s", width=5, style="Compact.TButton", command=lambda: self._seek_preview(-5)).grid(row=0, column=0, padx=(0, 6))
        self.preview_slider = ttk.Scale(seek_row, from_=0.0, to=1.0, orient="horizontal", variable=self.preview_time,
                                        command=lambda _v: self._update_preview_time_label(redraw=True))
        self.preview_slider.grid(row=0, column=1, sticky="ew")
        self.preview_slider.bind("<ButtonRelease-1>", lambda _e: self._refresh_preview())
        ttk.Button(seek_row, text="5s ▶", width=5, style="Compact.TButton", command=lambda: self._seek_preview(5)).grid(row=0, column=2, padx=(6, 8))
        self.preview_time_label = ttk.Label(seek_row, text="00:00 / 00:00", style="Muted.TLabel", width=15)
        self.preview_time_label.grid(row=0, column=3)
        ttk.Button(seek_row, text="Refresh Frame", command=self._refresh_preview).grid(row=0, column=4, padx=(6, 0))

        # ---------------- right: scrollable production tools
        tools_host = ttk.LabelFrame(main, text="Production Tools", padding=3)
        tools_host.rowconfigure(0, weight=1)
        tools_host.columnconfigure(0, weight=1)
        tools_canvas = tk.Canvas(tools_host, bg=BG, highlightthickness=0, width=320)
        tools_canvas.grid(row=0, column=0, sticky="nsew")
        tools_scroll = ttk.Scrollbar(tools_host, orient="vertical", command=tools_canvas.yview)
        tools_scroll.grid(row=0, column=1, sticky="ns")
        tools_canvas.configure(yscrollcommand=tools_scroll.set)
        tools = ttk.Frame(tools_canvas, padding=(8, 6, 10, 6))
        tools_window = tools_canvas.create_window((0, 0), window=tools, anchor="nw")
        tools.bind("<Configure>", lambda _e: tools_canvas.configure(scrollregion=tools_canvas.bbox("all")))
        tools_canvas.bind("<Configure>", lambda e: tools_canvas.itemconfigure(tools_window, width=e.width))
        self._bind_wheel(tools_canvas, tools_canvas)
        tools.columnconfigure(0, weight=1)

        def section(row: int, text: str) -> None:
            if row:
                ttk.Separator(tools).grid(row=row - 1, column=0, sticky="ew", pady=(2, 10))
            ttk.Label(tools, text=text, font=("Segoe UI Semibold", 10)).grid(row=row, column=0, sticky="w")

        def slider_row(row: int, label: str, from_: float, to: float, variable, on_change, value_label_attr: str,
                       text: str, pady=(3, 10)) -> ttk.Scale:
            frame = ttk.Frame(tools)
            frame.grid(row=row, column=0, sticky="ew", pady=pady)
            frame.columnconfigure(1, weight=1)
            ttk.Label(frame, text=label, width=9).grid(row=0, column=0, sticky="w")
            scale = ttk.Scale(frame, from_=from_, to=to, orient="horizontal", variable=variable, command=on_change)
            scale.grid(row=0, column=1, sticky="ew")
            value = ttk.Label(frame, text=text, width=6, anchor="e")
            value.grid(row=0, column=2, padx=(8, 0))
            setattr(self, value_label_attr, value)
            return scale

        section(0, "Video")
        self.zoom_slider = slider_row(1, "Zoom", 1.00, 1.25, self.zoom_factor, self._zoom_changed,
                                      "zoom_value_label", f"{float(self.zoom_factor.get()):.2f}x")
        self.zoom_slider.bind("<ButtonRelease-1>", lambda _e: self._refresh_preview())
        ttk.Checkbutton(self.zoom_slider.master, text="Flip (mirror left ↔ right)", variable=self.mirror_video,
                        command=self._mirror_changed).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._voice_volume_scale_var = tk.DoubleVar(value=float(self.voice_volume.get()))
        volume_frame = ttk.Frame(self.zoom_slider.master)
        volume_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        volume_frame.columnconfigure(1, weight=1)
        ttk.Label(volume_frame, text="Voice vol", width=9).grid(row=0, column=0, sticky="w")
        ttk.Scale(volume_frame, from_=50, to=300, orient="horizontal", variable=self._voice_volume_scale_var,
                  command=self._voice_volume_changed).grid(row=0, column=1, sticky="ew")
        self.voice_volume_label = ttk.Label(volume_frame, text="", width=13, anchor="e")
        self.voice_volume_label.grid(row=0, column=2, padx=(8, 0))
        self._voice_volume_changed(self.voice_volume.get())
        ttk.Checkbutton(self.zoom_slider.master, text="Short pauses (~0.25 s between lines)",
                        variable=self.short_pauses).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        frame_row = ttk.Frame(self.zoom_slider.master)
        frame_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        frame_row.columnconfigure(3, weight=1)
        ttk.Label(frame_row, text="Ratio", width=9).grid(row=0, column=0, sticky="w")
        self.main_ratio_combo = ttk.Combobox(frame_row, textvariable=self.output_ratio,
                                             values=OUTPUT_RATIOS, state="readonly", width=9)
        self.main_ratio_combo.grid(row=0, column=1, sticky="w")
        ttk.Label(frame_row, text="Fill").grid(row=0, column=2, sticky="e", padx=(10, 6))
        fill_combo = ttk.Combobox(frame_row, textvariable=self.frame_fill, values=tuple(FILL_LABELS.values()),
                                  state="readonly", width=15)
        fill_combo.grid(row=0, column=3, sticky="ew")
        for combo in (self.main_ratio_combo, fill_combo):
            combo.bind("<<ComboboxSelected>>", lambda _e: self._frame_changed())
        # Video quality: 720p / 480p render several times faster on PCs without an NVIDIA card.
        ttk.Label(frame_row, text="Quality", width=9).grid(row=1, column=0, sticky="w", pady=(6, 0))
        quality_combo = ttk.Combobox(frame_row, textvariable=self.quality, values=tuple(QUALITY_LABELS.values()),
                                     state="readonly", width=34)
        quality_combo.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        quality_combo.bind("<<ComboboxSelected>>", lambda _e: self._quality_changed())
        # Explicit one/two-ratio flow. The second ratio is rendered from the saved
        # script/voice, so the user never needs to guess what the button will reuse.
        ttk.Checkbutton(frame_row, text="Generate 2 ratios", variable=self.export_two_ratios,
                        command=self._second_ratio_changed).grid(row=2, column=0, columnspan=2,
                                                                 sticky="w", pady=(8, 0))
        self.second_ratio_combo = ttk.Combobox(frame_row, textvariable=self.second_output_ratio,
                                               values=OUTPUT_RATIOS, state="readonly", width=9)
        self.second_ratio_combo.grid(row=2, column=2, columnspan=2, sticky="w", pady=(8, 0))
        self.second_ratio_combo.bind("<<ComboboxSelected>>", lambda _e: self._second_ratio_changed())
        self.style_btn = ttk.Button(frame_row, text="EDIT 2ND STYLE", style="Compact.TButton",
                                    command=self._toggle_second_style, state="disabled")
        self.style_btn.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.style_banner = ttk.Label(frame_row, style="Muted.TLabel", text="")
        self.style_banner.grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 0))
        self._sync_second_ratio_state()
        ttk.Label(frame_row, style="Muted.TLabel", wraplength=250, justify="left",
                  text="One ratio: START renders once. Two ratios: START renders both; "
                       "the second uses the same script and voice. Each ratio keeps its own "
                       "text sizes and positions — use EDIT 2ND STYLE to customize it."
                  ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        section(3, "Blur Watermark Area")
        blur_row = ttk.Frame(tools)
        blur_row.grid(row=4, column=0, sticky="ew", pady=(5, 10))
        ttk.Button(blur_row, text="+ Draw Blur Box", style="Compact.TButton", command=self._begin_blur_box).pack(side="left")
        ttk.Button(blur_row, text="Delete Selected", style="Compact.TButton", command=self._delete_selected_blur).pack(side="left", padx=6)
        self.blur_count_label = ttk.Label(blur_row, text="0 box", style="Muted.TLabel")
        self.blur_count_label.pack(side="left", padx=(4, 0))

        section(6, "Title")
        self.title_entry = ttk.Entry(tools, textvariable=self.title_text, font=("Myanmar Text", 10))
        self.title_entry.grid(row=7, column=0, sticky="ew", pady=(5, 4))
        self.title_entry.bind("<KeyRelease>", lambda _e: self._redraw_editor_items())
        self._title_size_scale_var = tk.DoubleVar(value=float(self.title_size.get()))
        title_scale = slider_row(8, "Size", 24, 96, self._title_size_scale_var,
                                 lambda v: self._int_slider_changed(v, self.title_size, "title_size_label"),
                                 "title_size_label", str(int(self.title_size.get())), pady=(0, 2))
        title_scale.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        ttk.Label(tools, text="Drag the title on the preview to move it", style="Muted.TLabel").grid(row=9, column=0, sticky="w", pady=(0, 10))

        ttk.Separator(tools).grid(row=10, column=0, sticky="ew", pady=(2, 10))
        logo_header = ttk.Frame(tools)
        logo_header.grid(row=11, column=0, sticky="ew")
        logo_header.columnconfigure(0, weight=1)
        ttk.Label(logo_header, text="Logo", font=("Segoe UI Semibold", 10)).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(logo_header, text="Enable Logo", variable=self.logo_enabled, command=self._logo_enabled_changed).grid(row=0, column=1, sticky="e")
        logo_row = ttk.Frame(tools)
        logo_row.grid(row=12, column=0, sticky="ew", pady=(5, 4))
        logo_row.columnconfigure(0, weight=1)
        ttk.Entry(logo_row, textvariable=self.logo_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(logo_row, text="Browse", width=7, style="Compact.TButton", command=self._browse_logo).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(logo_row, text="Remove", width=7, style="Compact.TButton", command=self._remove_logo).grid(row=0, column=2, padx=(6, 0))
        self.logo_size_slider = slider_row(13, "Size", 0.06, 0.35, self.logo_width_frac, lambda _v: self._redraw_editor_items(),
                                           "logo_size_label", f"{self.logo_width_frac.get()*100:.0f}%", pady=(2, 2))
        self.logo_size_slider.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        self.logo_opacity_slider = slider_row(14, "Opacity", 0.10, 1.00, self.logo_opacity, lambda _v: self._logo_opacity_changed(),
                                              "logo_opacity_label", f"{self.logo_opacity.get()*100:.0f}%", pady=(2, 10))
        self.logo_opacity_slider.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())

        section(16, "Extra Text Box")
        self.extra_text_entry = ttk.Entry(tools, textvariable=self.extra_text, font=("Myanmar Text", 10))
        self.extra_text_entry.grid(row=17, column=0, sticky="ew", pady=(5, 4))
        self.extra_text_entry.bind("<KeyRelease>", lambda _e: self._redraw_editor_items())
        self._extra_size_scale_var = tk.DoubleVar(value=float(self.extra_text_size.get()))
        extra_scale = slider_row(18, "Size", 18, 84, self._extra_size_scale_var,
                                 lambda v: self._int_slider_changed(v, self.extra_text_size, "extra_size_label"),
                                 "extra_size_label", str(int(self.extra_text_size.get())), pady=(0, 2))
        extra_scale.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        self.extra_opacity_slider = slider_row(19, "Opacity", 0.10, 1.00, self.extra_text_opacity, lambda _v: self._extra_opacity_changed(),
                                               "extra_opacity_label", f"{self.extra_text_opacity.get()*100:.0f}%", pady=(2, 2))
        self.extra_opacity_slider.bind("<ButtonRelease-1>", lambda _e: self._redraw_editor_items())
        ttk.Label(tools, text="Drag the text on the preview to move it", style="Muted.TLabel").grid(row=20, column=0, sticky="w", pady=(0, 10))
        section(22, "Narration Subtitle")
        self.subtitle_controls = SubtitleControls(tools, self.cfg.get("subtitles"), self._subtitle_changed)
        self.subtitle_controls.grid(row=23, column=0, sticky="ew", pady=(5, 10))
        if self._target_code() == "en":
            try:
                self.subtitle_controls.set_font_choices(latin_fonts())
            except Exception:
                pass

        # ================= V2 production tools (spec §3-§10) =================
        # One-click viral formats (§9.2)
        viral = ttk.Frame(tools)
        viral.grid(row=30, column=0, sticky="ew", pady=(2, 10))
        for i, (label, _fmt) in enumerate(VIRAL_FORMATS):
            ttk.Button(viral, text=label, style="Compact.TButton",
                       command=lambda name=label: self._apply_viral_format(name)).grid(
                           row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 4, 0), pady=2)
        viral.columnconfigure(0, weight=1)
        viral.columnconfigure(1, weight=1)

        # ---- Crop (§5) ----
        section(32, "Crop — remove black bars / watermarks")
        crop_box = ttk.Frame(tools)
        crop_box.grid(row=33, column=0, sticky="ew", pady=(4, 2))
        crop_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(crop_box, text="Enable crop", variable=self.crop_enabled,
                        command=self._crop_toggled).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(crop_box, text="Auto-detect black bars", style="Compact.TButton",
                   command=self._auto_detect_crop).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(crop_box, text="Reset (full frame)", style="Compact.TButton",
                   command=self._reset_crop).grid(row=1, column=1, sticky="w", pady=(5, 0), padx=(5, 0))
        ttk.Label(crop_box, text="Aspect lock").grid(row=2, column=0, sticky="w", pady=(5, 0))
        ttk.Combobox(crop_box, textvariable=self.crop_aspect, values=ASPECT_OPTIONS,
                     state="readonly", width=10).grid(row=2, column=1, sticky="w", pady=(5, 0), padx=(5, 0))
        for r, (text, var, low, high) in enumerate((
                ("X", self.crop_x, 0.0, 1.0), ("Y", self.crop_y, 0.0, 1.0),
                ("Width", self.crop_w, 0.02, 1.0), ("Height", self.crop_h, 0.02, 1.0)), start=3):
            ttk.Label(crop_box, text=text, width=7).grid(row=r, column=0, sticky="w")
            ttk.Scale(crop_box, from_=low, to=high, orient="horizontal", variable=var,
                      command=lambda _v: self._crop_slider_changed()).grid(row=r, column=1, sticky="ew", pady=1)
        self.crop_dim_label = ttk.Label(crop_box, text="", style="Muted.TLabel")
        self.crop_dim_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(tools, text="Drag the crop box / its handles on the preview",
                  style="Muted.TLabel").grid(row=34, column=0, sticky="w", pady=(0, 8))

        # ---- Color correction (§6) ----
        section(36, "Color Correction")
        color_box = ttk.Frame(tools)
        color_box.grid(row=37, column=0, sticky="ew", pady=(4, 8))
        color_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(color_box, text="Enable", variable=self.color_enabled,
                        command=self._color_changed).grid(row=0, column=0, columnspan=3, sticky="w")
        for r, (text, var, low, high, fmt) in enumerate((
                ("Brightness", self.brightness, -0.5, 0.5, "{:+.2f}"),
                ("Contrast", self.contrast, 0.5, 2.0, "{:.2f}"),
                ("Saturation", self.saturation, 0.0, 3.0, "{:.2f}")), start=1):
            ttk.Label(color_box, text=text, width=10).grid(row=r, column=0, sticky="w")
            ttk.Scale(color_box, from_=low, to=high, orient="horizontal", variable=var,
                      command=lambda _v: self._color_changed()).grid(row=r, column=1, sticky="ew", pady=1)
            lbl = ttk.Label(color_box, text=fmt.format(var.get()), width=6, anchor="e")
            lbl.grid(row=r, column=2, padx=(6, 0))
            var.trace_add("write", lambda *_a, v=var, l=lbl, f=fmt: self._safe_label(l, f.format(float(v.get()))))
        ttk.Button(color_box, text="Reset to Default", style="Compact.TButton",
                   command=self._reset_color).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---- Custom image overlays (§4) ----
        section(39, "Custom Image Overlays")
        ov_head = ttk.Frame(tools)
        ov_head.grid(row=40, column=0, sticky="ew", pady=(4, 4))
        ttk.Button(ov_head, text="+ Add Image", style="Compact.TButton",
                   command=self._add_custom_overlay).pack(side="left")
        self.overlay_count_label = ttk.Label(ov_head, text="0 / 5", style="Muted.TLabel")
        self.overlay_count_label.pack(side="left", padx=(8, 0))
        self.overlays_host = ttk.Frame(tools)
        self.overlays_host.grid(row=41, column=0, sticky="ew", pady=(0, 8))
        self.overlays_host.columnconfigure(0, weight=1)
        self._rebuild_overlay_rows()

        # ---- Audio ducking (§8) ----
        section(43, "Audio Ducking")
        duck_box = ttk.Frame(tools)
        duck_box.grid(row=44, column=0, sticky="ew", pady=(4, 8))
        duck_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(duck_box, text="Auto-duck background audio during narration",
                        variable=self.ducking_enabled).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(duck_box, text="Duck level", width=10).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Scale(duck_box, from_=10, to=80, orient="horizontal", variable=self.duck_volume_percent,
                  command=lambda _v: self.duck_value_label.configure(
                      text=f"{int(self.duck_volume_percent.get())}%")).grid(row=1, column=1, sticky="ew", pady=(5, 0))
        self.duck_value_label = ttk.Label(duck_box, text=f"{self.duck_volume_percent.get()}%", width=6, anchor="e")
        self.duck_value_label.grid(row=1, column=2, padx=(6, 0), pady=(5, 0))
        ttk.Label(duck_box, text="How quiet the original background gets while the narrator speaks",
                  style="Muted.TLabel", wraplength=280, justify="left").grid(row=2, column=0, columnspan=3, sticky="w")

        # ---- Batch queue (§10) ----
        section(46, "Batch Queue")
        self.skip_completed = tk.BooleanVar(value=bool(self.cfg.get("batch_skip_completed", True)))
        self.batch_manager.skip_completed = bool(self.skip_completed.get())
        batch_box = ttk.Frame(tools)
        batch_box.grid(row=47, column=0, sticky="ew", pady=(4, 8))
        batch_box.columnconfigure(0, weight=1)
        cols = ("name", "status", "progress", "duration", "error")
        self.batch_tree = ttk.Treeview(batch_box, columns=cols, show="headings", height=6)
        for col, text, width in (("name", "Filename", 150), ("status", "Status", 90),
                                 ("progress", "Progress", 62), ("duration", "Duration", 66),
                                 ("error", "Note", 110)):
            self.batch_tree.heading(col, text=text)
            self.batch_tree.column(col, width=width, anchor="w", stretch=(col in ("name", "error")))
        batch_scroll = ttk.Scrollbar(batch_box, orient="vertical", command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=batch_scroll.set)
        self.batch_tree.grid(row=0, column=0, sticky="ew")
        batch_scroll.grid(row=0, column=1, sticky="ns")
        batch_btns = ttk.Frame(batch_box)
        batch_btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        for text, cmd in (("↑ Up", self._batch_move_up), ("↓ Down", self._batch_move_down),
                          ("🗑️ Remove", self._batch_remove), ("🔄 Retry Failed", self._batch_retry)):
            ttk.Button(batch_btns, text=text, style="Compact.TButton", command=cmd).pack(side="left", padx=(0, 4))
        ttk.Checkbutton(batch_box, text="Skip already completed", variable=self.skip_completed,
                        command=self._skip_completed_changed).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.batch_summary = ttk.Label(batch_box, text="No videos queued", style="Muted.TLabel")
        self.batch_summary.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.batch_tree.bind("<Button-3>", self._batch_context_menu)
        # Drag-to-reorder the queue (spec §10 BQ-05).
        self.batch_tree.bind("<ButtonPress-1>", self._batch_drag_start, add="+")
        self.batch_tree.bind("<ButtonRelease-1>", self._batch_drag_release, add="+")
        self.batch_menu = tk.Menu(self, tearoff=0)
        self.batch_menu.add_command(label="Preview video", command=self._batch_preview)
        self.batch_menu.add_command(label="Open output folder", command=self._batch_open_folder)
        self.batch_menu.add_command(label="Retry", command=self._batch_retry_selected)
        self.batch_menu.add_command(label="Remove", command=self._batch_remove)
        self._batch_menu_index = 0
        self._batch_drag_iid: str | None = None
        self._bind_wheel_recursive(tools, tools_canvas)
        # Job resume / config loads set the IntVars directly; keep the sliders in step.
        self.title_size.trace_add("write", lambda *_a: self._sync_int_slider(self.title_size, self._title_size_scale_var, "title_size_label"))
        self.extra_text_size.trace_add("write", lambda *_a: self._sync_int_slider(self.extra_text_size, self._extra_size_scale_var, "extra_size_label"))

        main.add(left, weight=3)
        main.add(preview_box, weight=6)
        main.add(tools_host, weight=2)
        self._main_pane, self._left_pane = main, left
        self.after(250, self._apply_pane_layout)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        controls = ttk.Frame(outer)
        controls.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        controls.columnconfigure(0, weight=1)
        btns = ttk.Frame(controls)
        btns.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.start_btn = ttk.Button(btns, text="START / RESUME", style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="left")
        self._update_start_button_label()
        self.stop_btn = ttk.Button(btns, text="STOP SAFELY", style="Danger.TButton", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.rerender_btn = ttk.Button(btns, text="RENDER THIS RATIO", command=self._render_another_ratio,
                                       state="disabled")
        self.rerender_btn.pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Open Job Folder", command=self._open_output).pack(side="left")
        ttk.Button(btns, text="Edit Text", command=self._open_text_editor).pack(side="left", padx=(8, 0))
        ttk.Label(btns, textvariable=self.job_display, style="Muted.TLabel").pack(side="left", padx=(12, 0))
        ttk.Label(btns, textvariable=self.stage, style="Muted.TLabel").pack(side="right", padx=(8, 0))
        ttk.Label(btns, textvariable=self.status, style="Status.TLabel").pack(side="right")

        self.progress = ttk.Progressbar(controls, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew")

    # ---------------------------------------------------------------- layout helpers
    UI_LAYOUT_FILE = LOGS_DIR / "ui_layout.json"

    @staticmethod
    def _bind_wheel(widget: tk.Misc, canvas: tk.Canvas) -> None:
        widget.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"), add="+")

    def _bind_wheel_recursive(self, widget: tk.Misc, canvas: tk.Canvas) -> None:
        # Sliders and combo/spin boxes keep their own wheel behaviour.
        if not isinstance(widget, (ttk.Scale, ttk.Combobox, ttk.Spinbox)):
            self._bind_wheel(widget, canvas)
        for child in widget.winfo_children():
            self._bind_wheel_recursive(child, canvas)

    def _apply_pane_layout(self) -> None:
        """Default sashes (settings pane ~38%, tools ~19%); a layout dragged last time wins."""
        try:
            self.update_idletasks()
            width = self._main_pane.winfo_width()
            height = self._left_pane.winfo_height()
            if width < 400 or height < 200:
                self.after(250, self._apply_pane_layout)
                return
            try:
                saved = json.loads(self.UI_LAYOUT_FILE.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
            left_w = int(saved.get("left_fraction", 0.38) * width)
            tools_w = int(saved.get("tools_fraction", 0.19) * width)
            left_w = max(520, min(left_w, width - tools_w - 420))
            self._main_pane.sashpos(0, left_w)
            self._main_pane.sashpos(1, max(left_w + 420, width - tools_w))
            setup_h = int(saved.get("setup_fraction", 0) * height) or int(height * 0.62)
            self._left_pane.sashpos(0, max(160, min(setup_h, height - 120)))
        except Exception as exc:
            self._log(f"⚠️ Layout restore skipped: {exc}")

    def _save_pane_layout(self) -> None:
        try:
            width = max(1, self._main_pane.winfo_width())
            height = max(1, self._left_pane.winfo_height())
            data = {
                "left_fraction": round(self._main_pane.sashpos(0) / width, 4),
                "tools_fraction": round((width - self._main_pane.sashpos(1)) / width, 4),
                "setup_fraction": round(self._left_pane.sashpos(0) / height, 4),
            }
            self.UI_LAYOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.UI_LAYOUT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        self._save_pane_layout()
        self.splitter_tab.shutdown()
        try:
            self.cfg["last_tool_tab"] = "splitter" if self.tools_tabs.select() == str(self.splitter_tab) else "recap"
            save_config(self.cfg)
        except Exception:
            pass
        self.destroy()

    def _voice_volume_changed(self, value) -> None:
        import math
        percent = int(round(float(value) / 5.0) * 5)  # 5% steps
        self.voice_volume.set(percent)
        self.voice_volume_label.configure(text=f"{percent}% ({20 * math.log10(percent / 100.0):+.1f}dB)")

    def _mirror_changed(self) -> None:
        # Persist right away so the choice survives a restart even before START.
        self.cfg["mirror_video"] = bool(self.mirror_video.get())
        save_config(self.cfg)
        self._log(f"🔁 Flip / mirror {'ON' if self.mirror_video.get() else 'OFF'} — video parts re-render on next run; TTS is reused.")
        self._refresh_preview()

    def _sync_int_slider(self, source: tk.IntVar, scale_var: tk.DoubleVar, label_attr: str) -> None:
        try:
            number = int(source.get())
        except (tk.TclError, ValueError):
            return
        scale_var.set(float(number))
        getattr(self, label_attr).configure(text=str(number))

    def _int_slider_changed(self, value: str, target: tk.IntVar, label_attr: str) -> None:
        number = int(round(float(value)))
        target.set(number)
        getattr(self, label_attr).configure(text=str(number))
        self._redraw_editor_items()

    def _preview_resized(self, event) -> None:
        size = (max(160, int(event.width) - 2), max(90, int(event.height) - 2))
        if size == self._preview_canvas_size:
            return
        self._preview_canvas_size = size
        self._redraw_editor_items()
        # Re-extract the frame only after the user stops resizing.
        if self._preview_resize_job is not None:
            self.after_cancel(self._preview_resize_job)
        self._preview_resize_job = self.after(300, self._refresh_preview)

    def _seek_preview(self, seconds: float) -> None:
        upper = float(self.preview_duration or 0.0)
        value = float(self.preview_time.get()) + seconds
        self.preview_time.set(max(0.0, min(value, max(0.0, upper - 0.05)) if upper else max(0.0, value)))
        self._update_preview_time_label()
        self._refresh_preview()

    def _update_preview_time_label(self, redraw: bool = False) -> None:
        def fmt(sec: float) -> str:
            sec = max(0, int(sec))
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        if hasattr(self, "preview_time_label"):
            self.preview_time_label.configure(text=f"{fmt(self.preview_time.get())} / {fmt(self.preview_duration or 0)}")
        if redraw:
            # Timed image overlays must appear/disappear while the user scrubs,
            # not only after the frame is re-extracted on mouse release.
            self._redraw_custom_overlay_items()

    def _edge_rate_changed(self, value: str) -> None:
        try:
            self.edge_speed_value.configure(text=_format_edge_rate(value))
        except Exception:
            pass

    def _create_log_tab(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.log_notebook)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        box = tk.Text(
            frame, wrap="word", width=36, height=12, bg="#080a0e", fg="#d9dee8",
            insertbackground=FG, selectbackground="#274d8b", selectforeground="#ffffff",
            relief="flat", borderwidth=0, padx=8, pady=8, font=("Consolas", 9),
        )
        box.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=box.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        box.configure(yscrollcommand=scroll.set)
        self.log_notebook.add(frame, text=title)
        return box

    def _zoom_changed(self, value: str) -> None:
        try:
            self.zoom_value_label.configure(text=f"{float(value):.2f}x")
        except Exception:
            pass

    def _logo_opacity_changed(self) -> None:
        try:
            self.logo_opacity_label.configure(text=f"{float(self.logo_opacity.get())*100:.0f}%")
        except Exception:
            pass

    def _extra_opacity_changed(self) -> None:
        try:
            self.extra_opacity_label.configure(text=f"{float(self.extra_text_opacity.get())*100:.0f}%")
        except Exception:
            pass

    def _persist_logo_choice(self) -> None:
        """Persist logo enable/path immediately so a removed logo cannot return on restart."""
        try:
            self.cfg["logo_enabled"] = bool(self.logo_enabled.get())
            self.cfg["logo_path"] = self.logo_path.get().strip() if self.logo_enabled.get() else ""
            save_config(self.cfg)
        except Exception as exc:
            self._log(f"⚠️ Could not save logo setting: {exc}")

    def _logo_enabled_changed(self) -> None:
        # OFF means no logo is passed to the renderer even when a legacy path still exists.
        self._persist_logo_choice()
        self._redraw_editor_items()

    def _remove_logo(self) -> None:
        # Full removal: clear both the toggle and saved path so app/job resume cannot revive it.
        self.logo_enabled.set(False)
        self.logo_path.set("")
        self._logo_photo = None
        self._persist_logo_choice()
        self._redraw_editor_items()
        self.status.set("LOGO REMOVED")
        self._log("🧹 Logo removed and saved setting cleared.")

    def _browse_logo(self) -> None:
        p = filedialog.askopenfilename(title="Select logo", filetypes=[("Image", "*.png *.webp *.jpg *.jpeg"), ("All", "*.*")])
        if p:
            self.logo_path.set(p)
            self.logo_enabled.set(True)
            self._persist_logo_choice()
            self._redraw_editor_items()

    def _begin_blur_box(self) -> None:
        self._draw_blur_mode = True
        self.selected_blur = None
        self.preview_canvas.configure(cursor="crosshair")
        self.status.set("DRAW BLUR BOX")

    def _delete_selected_blur(self) -> None:
        if self.selected_blur is None:
            return
        if 0 <= self.selected_blur < len(self.blur_boxes):
            self.blur_boxes.pop(self.selected_blur)
        self.selected_blur = None
        self._redraw_editor_items()

    def _fill_code(self) -> str:
        return next((code for code, label in FILL_LABELS.items() if label == self.frame_fill.get()), "blur")

    @staticmethod
    def _ratio_label(value: object) -> str:
        """Accept either a UI ratio label (9:16) or a saved key (9x16)."""
        text = str(value or "16:9")
        for candidate in OUTPUT_RATIOS:
            if ratio_key(candidate) == ratio_key(text):
                return candidate
        return text

    def _update_start_button_label(self) -> None:
        if not hasattr(self, "start_btn"):
            return
        batch_count = len(getattr(self, "batch_files", []) or [])
        two = bool(self.export_two_ratios.get())
        if batch_count:
            text = f"START BATCH + 2 RATIOS ({batch_count})" if two else f"START BATCH ({batch_count})"
        else:
            text = "START + 2 RATIOS" if two else "START / RESUME"
        self.start_btn.configure(text=text)

    def _sync_second_ratio_state(self) -> None:
        """The second Ratio selector / style editor is only usable in two-ratio mode."""
        two = bool(self.export_two_ratios.get())
        editing = bool(getattr(self, "editing_second_ratio", False))
        if hasattr(self, "main_ratio_combo") and not editing:
            self.main_ratio_combo.configure(state="readonly")
        if hasattr(self, "second_ratio_combo") and not editing:
            self.second_ratio_combo.configure(state="readonly" if two else "disabled")
        if hasattr(self, "style_btn"):
            self.style_btn.configure(state="normal" if two else "disabled")
        if hasattr(self, "style_banner") and not editing:
            self.style_banner.configure(text="")

    def _ensure_profile(self, ratio: str) -> None:
        """Give a ratio its own starting style if the user has not saved one."""
        key = ratio_key(ratio)
        profiles = dict(self.cfg.get("ratio_profiles") or {})
        current = profiles.get(key)
        if isinstance(current, dict) and current:
            return
        profiles[key] = default_profile_for_ratio(ratio)
        self.cfg["ratio_profiles"] = profiles
        save_config(self.cfg)
        self._log(f"🎚️ {ratio} started with its own recommended text sizes / positions.")

    def _ensure_second_profile(self, second: str) -> None:
        self._ensure_profile(second)

    def _second_ratio_changed(self) -> None:
        """Keep the two ratio choices distinct and make the flow visible on the button."""
        primary = self._ratio_label(self.output_ratio.get())
        second = self._ratio_label(self.second_output_ratio.get())
        if second == primary:
            second = "16:9" if primary != "16:9" else "9:16"
            self.second_output_ratio.set(second)
        self.cfg["export_two_ratios"] = bool(self.export_two_ratios.get())
        self.cfg["second_output_ratio"] = second
        self._ensure_second_profile(second)
        save_config(self.cfg)
        self._sync_second_ratio_state()
        self._update_start_button_label()
        if self.export_two_ratios.get():
            self._log(f"🎬 Two-ratio job: {primary} (full run) + {second} (saved voice, render-only).")
            self._log("   The second ratio has a separate saved style — click EDIT 2ND STYLE to change it.")

    def _toggle_second_style(self) -> None:
        if getattr(self, "editing_second_ratio", False):
            self._done_second_style()
        else:
            self._edit_second_style()

    def _edit_second_style(self) -> None:
        """Temporarily show the second ratio in all style controls, then save it."""
        if not self.export_two_ratios.get():
            return
        main = self._ratio_label(self.output_ratio.get())
        second = self._ratio_label(self.second_output_ratio.get())
        if second == main:
            self._second_ratio_changed()
            second = self._ratio_label(self.second_output_ratio.get())
        self._save_ratio_profile(main)          # never lose the main ratio's edits
        self._ensure_profile(second)            # start from its own recommended style
        self._main_ratio_before_style_edit = main
        self.editing_second_ratio = True
        self.output_ratio.set(second)
        self._active_ratio_key = ratio_key(second)
        self._apply_ratio_profile(second, quiet=False)
        self.main_ratio_combo.configure(state="disabled")
        self.second_ratio_combo.configure(state="disabled")
        self.style_btn.configure(text="DONE 2ND STYLE")
        self.style_banner.configure(
            text=f"Editing {second} style — adjust Title / Subtitle / Extra Text, then click DONE 2ND STYLE.")
        self.status.set(f"EDIT {second} STYLE")
        self._log(f"🎚️ Editing {second} style. Main Ratio {main} will be restored when you click DONE.")
        self._redraw_editor_items()
        self._refresh_preview()

    def _done_second_style(self) -> None:
        """Save the second ratio's style and return the GUI to the main ratio."""
        if not getattr(self, "editing_second_ratio", False):
            return
        second = self._ratio_label(self.output_ratio.get())
        self._save_ratio_profile(second)
        main = self._ratio_label(self._main_ratio_before_style_edit or second)
        self.output_ratio.set(main)
        self._active_ratio_key = ratio_key(main)
        self._apply_ratio_profile(main, quiet=True)
        self.editing_second_ratio = False
        self._main_ratio_before_style_edit = None
        self._sync_second_ratio_state()
        self.style_btn.configure(text="EDIT 2ND STYLE")
        self.status.set("READY")
        self._log(f"🎚️ {second} style saved separately; back to main Ratio {main}.")
        self._redraw_editor_items()
        self._refresh_preview()

    def _output_canvas(self) -> tuple[int, int]:
        """Size of the final frame for the chosen Ratio (Original follows the loaded video)."""
        return output_canvas(self.output_ratio.get(), *self.source_size)

    def _frame_changed(self) -> None:
        # Save what the user just adjusted for the old ratio, then restore the
        # saved text sizes/positions for the newly selected ratio.
        previous = self._active_ratio_key
        self._save_ratio_profile(previous)
        self._active_ratio_key = ratio_key(self.output_ratio.get())
        self._ensure_profile(self.output_ratio.get())
        self._apply_ratio_profile(self.output_ratio.get(), quiet=True)
        if self._ratio_label(self.second_output_ratio.get()) == self._ratio_label(self.output_ratio.get()):
            self._second_ratio_changed()
        self._redraw_editor_items()
        self._refresh_preview()

    def _capture_ratio_profile(self, ratio: str | None = None) -> dict:
        """Current visual settings, tagged with ``ratio``, without validating a job."""
        try:
            settings = self.preset_manager.capture_from_controller(self)
        except Exception:
            settings = {
                "output_ratio": self.output_ratio.get(),
                "frame_fill": self._fill_code(),
                "video_quality": self._quality_code(),
                "title_size": int(self.title_size.get()),
                "extra_text_size": int(self.extra_text_size.get()),
                "subtitles": self.subtitle_controls.values(),
            }
        if ratio:
            settings["output_ratio"] = self._ratio_label(ratio)
        return profile_from_settings(settings)

    def _save_ratio_profile(self, ratio: str | None = None, settings: dict | None = None) -> None:
        profile = profile_from_settings(settings) if settings is not None else self._capture_ratio_profile(ratio)
        key = ratio_key(profile.get("output_ratio") or ratio or self.output_ratio.get())
        profiles = dict(self.cfg.get("ratio_profiles") or {})
        profiles[key] = profile
        self.cfg["ratio_profiles"] = profiles
        self._active_ratio_key = key
        save_config(self.cfg)

    def _apply_ratio_profile(self, ratio: str | None = None, quiet: bool = False) -> bool:
        """Restore saved title/subtitle/extra-text sizes and positions for a ratio."""
        wanted = self._ratio_label(ratio or self.output_ratio.get())
        key = ratio_key(wanted)
        profiles = self.cfg.get("ratio_profiles") or {}
        profile = profiles.get(key) or profiles.get(wanted)
        if not isinstance(profile, dict) or not profile:
            self._active_ratio_key = key
            return False

        def set_var(attr: str, value) -> None:
            try:
                getattr(self, attr).set(value)
            except Exception:
                pass

        if "output_ratio" in profile:
            set_var("output_ratio", str(profile["output_ratio"]))
        if "frame_fill" in profile:
            set_var("frame_fill", self._fill_label(str(profile["frame_fill"])))
        if "video_quality" in profile:
            set_var("quality", self._quality_label(str(profile["video_quality"])))
        for attr in ("zoom_factor", "title_text", "title_size", "extra_text",
                     "extra_text_size", "extra_text_opacity", "logo_path"):
            if attr in profile:
                set_var(attr, profile[attr])
        for attr in ("mirror_video", "logo_enabled"):
            if attr in profile:
                set_var(attr, bool(profile[attr]))
        for attr, key_name in (("title_pos", "title_position"), ("logo_pos", "logo_position"),
                               ("extra_text_pos", "extra_text_position")):
            if key_name in profile and hasattr(self, attr):
                setattr(self, attr, list(profile[key_name]))
        if "logo_width_frac" in profile:
            set_var("logo_width_frac", float(profile["logo_width_frac"]))
        if "logo_opacity" in profile:
            set_var("logo_opacity", float(profile["logo_opacity"]))
        if "crop" in profile and hasattr(self, "_apply_crop_values"):
            self._apply_crop_values(dict(profile["crop"] or {}))
        if "color_correction" in profile and hasattr(self, "_apply_color_values"):
            self._apply_color_values(dict(profile["color_correction"] or {}))
        if "audio_ducking" in profile and hasattr(self, "_apply_ducking_values"):
            self._apply_ducking_values(dict(profile["audio_ducking"] or {}))
        if "custom_overlays" in profile and hasattr(self, "custom_overlays"):
            self.custom_overlays = [normalize_overlay(o, i + 1) for i, o in
                                    enumerate((profile["custom_overlays"] or [])[:MAX_CUSTOM_OVERLAYS])]
            self._rebuild_overlay_rows()
        if "subtitles" in profile and hasattr(self, "subtitle_controls"):
            try:
                self.subtitle_controls.load_values(dict(profile["subtitles"] or {}))
            except Exception:
                pass
        self._sync_preset_ui()
        self._active_ratio_key = key
        if not quiet:
            self._log(f"🎚️ Ratio {wanted}: saved text sizes / positions restored")
        return True

    def _preview_bounds(self) -> tuple[int, int, int, int]:
        """Largest output-shaped frame (Ratio) that fits the canvas: (x offset, y offset, width, height).

        Overlay positions are stored as 0..1 fractions of the output frame, so the preview
        can be any size without moving where Title/Logo/Blur land in the render.
        """
        cw, ch = getattr(self, "_preview_canvas_size", (PREVIEW_W, PREVIEW_H))
        fw, fh = self._output_canvas() if hasattr(self, "output_ratio") else (1920, 1080)
        scale = min(cw / fw, ch / fh)
        width, height = max(16, int(fw * scale)) // 2 * 2, max(16, int(fh * scale)) // 2 * 2
        return (cw - width) // 2, (ch - height) // 2, width, height

    def _preview_xy(self, event) -> tuple[float, float]:
        ox, oy, width, height = self._preview_bounds()
        return (max(float(ox), min(float(event.x), ox + width)),
                max(float(oy), min(float(event.y), oy + height)))

    def _refresh_preview(self) -> None:
        src = self.source.get().strip()
        if not src or not Path(src).is_file():
            return
        try:
            ffmpeg = _tool_path(self.cfg, "ffmpeg")
            ox, oy, width, height = self._preview_bounds()
            out = LOGS_DIR / "preview_frame.png"
            fw, fh = self.source_size if self.source_size[0] else (1920, 1080)
            prefilter, eff_size = build_prefilter(self._crop_values(), self._color_values(), fw, fh)
            extract_preview_frame(
                ffmpeg, src, float(self.preview_time.get()), float(self.zoom_factor.get()),
                bool(self.mirror_video.get()), str(out), width, height,
                ratio=self.output_ratio.get(), fill=self._fill_code(), source_size=eff_size,
                prefilter=prefilter,
            )
            self._preview_photo = tk.PhotoImage(file=str(out))
            self.preview_canvas.delete("frame")
            self.preview_canvas.create_image(ox, oy, image=self._preview_photo, anchor="nw", tags=("frame",))
            self.preview_canvas.tag_lower("frame")
            self._redraw_editor_items()
            self._update_preview_time_label()
        except Exception as exc:
            self._log(f"⚠️ Preview frame error: {exc}")

    def _redraw_editor_items(self) -> None:
        if not hasattr(self, "preview_canvas"):
            return
        c = self.preview_canvas
        c.delete("editor")
        ox, oy, pw, ph = self._preview_bounds()
        self._draw_crop_overlay(c, ox, oy, pw, ph)
        self._draw_subtitle_preview(ox, oy, pw, ph)
        self.blur_count_label.configure(text=f"{len(self.blur_boxes)} box" + ("es" if len(self.blur_boxes) != 1 else ""))
        for i, b in enumerate(self.blur_boxes):
            x1, y1 = ox + b["x"] * pw, oy + b["y"] * ph
            x2, y2 = ox + (b["x"] + b["w"]) * pw, oy + (b["y"] + b["h"]) * ph
            color = "#ffd166" if i == self.selected_blur else "#48d7ff"
            c.create_rectangle(x1, y1, x2, y2, outline=color, width=2, dash=(6, 4), tags=("editor", "blur", f"blur_{i}"))
            c.create_text(x1 + 5, y1 + 5, text="BLUR", fill=color, anchor="nw", font=("Segoe UI Semibold", 9), tags=("editor", f"blur_{i}"))
            if i == self.selected_blur:
                c.create_rectangle(x2-5, y2-5, x2+5, y2+5, fill=color, outline="#111111", tags=("editor", f"blur_handle_{i}"))

        txt = self.title_text.get().strip()
        if txt:
            px = ox + float(self.title_pos[0]) * pw
            py = oy + float(self.title_pos[1]) * ph
            fs = max(10, int(self.title_size.get() * pw / self._output_canvas()[0]))
            c.create_text(px+2, py+2, text=txt, fill="#000000", anchor="center", font=("Myanmar Text", fs, "bold"), width=int(pw*0.82), tags=("editor", "title"))
            c.create_text(px, py, text=txt, fill="#ffffff", anchor="center", font=("Myanmar Text", fs, "bold"), width=int(pw*0.82), tags=("editor", "title"))

        extra_txt = self.extra_text.get().strip()
        if extra_txt:
            ex = ox + float(self.extra_text_pos[0]) * pw
            ey = oy + float(self.extra_text_pos[1]) * ph
            efs = max(9, int(self.extra_text_size.get() * pw / self._output_canvas()[0]))
            opacity = max(0.10, min(1.00, float(self.extra_text_opacity.get())))
            shade = max(140, min(255, int(255 * opacity)))
            fill = f"#{shade:02x}{shade:02x}{shade:02x}"
            c.create_text(ex+2, ey+2, text=extra_txt, fill="#101010", anchor="center", font=("Myanmar Text", efs, "bold"), width=int(pw*0.78), tags=("editor", "extra_text"))
            c.create_text(ex, ey, text=extra_txt, fill=fill, anchor="center", font=("Myanmar Text", efs, "bold"), width=int(pw*0.78), tags=("editor", "extra_text"))

        logo = self.logo_path.get().strip() if self.logo_enabled.get() else ""
        if logo and Path(logo).is_file():
            try:
                ffmpeg = _tool_path(self.cfg, "ffmpeg")
                lw = max(36, int(pw * float(self.logo_width_frac.get())))
                tmp = LOGS_DIR / "preview_logo.png"
                scale_logo_preview(ffmpeg, logo, lw, str(tmp))
                self._logo_photo = tk.PhotoImage(file=str(tmp))
                self.logo_size_label.configure(text=f"{float(self.logo_width_frac.get())*100:.0f}%")
                x, y = ox + float(self.logo_pos[0])*pw, oy + float(self.logo_pos[1])*ph
                c.create_image(x, y, image=self._logo_photo, anchor="nw", tags=("editor", "logo"))
            except Exception as exc:
                self._log(f"⚠️ Logo preview error: {exc}")
        self._draw_custom_overlays(c, ox, oy, pw, ph)

    # ---- V2 preview: crop box and draggable custom overlays ----------------
    def _draw_crop_overlay(self, c, ox, oy, pw, ph) -> None:
        if not self.crop_enabled.get():
            return
        crop = self._crop_values()
        x1 = ox + crop["x_percent"] * pw
        y1 = oy + crop["y_percent"] * ph
        x2 = x1 + crop["width_percent"] * pw
        y2 = y1 + crop["height_percent"] * ph
        shade = {"fill": "#000000", "stipple": "gray50", "outline": "", "tags": ("editor", "crop_shade")}
        c.create_rectangle(ox, oy, ox + pw, y1, **shade)
        c.create_rectangle(ox, y2, ox + pw, oy + ph, **shade)
        c.create_rectangle(ox, y1, x1, y2, **shade)
        c.create_rectangle(x2, y1, ox + pw, y2, **shade)
        c.create_rectangle(x1, y1, x2, y2, outline="#7be0a4", width=2,
                           tags=("editor", "crop", "crop_box"))
        handles = ((x1, y1), ((x1 + x2) / 2, y1), (x2, y1),
                   (x1, (y1 + y2) / 2), (x2, (y1 + y2) / 2),
                   (x1, y2), ((x1 + x2) / 2, y2), (x2, y2))
        for i, (hx, hy) in enumerate(handles):
            c.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill="#7be0a4", outline="#08301b",
                               tags=("editor", "crop", f"crop_handle_{i}"))
        c.create_text(x1 + 6, y1 + 7, text="CROP · drag edges / corners", fill="#7be0a4",
                      anchor="nw", font=("Segoe UI", 9, "bold"), tags=("editor", "crop"))

    def _redraw_custom_overlay_items(self) -> None:
        """Repaint only the timed image overlays while the preview time changes."""
        c = getattr(self, "preview_canvas", None)
        if c is None:
            return
        c.delete("custom_overlay")
        ox, oy, pw, ph = self._preview_bounds()
        self._draw_custom_overlays(c, ox, oy, pw, ph)

    def _draw_custom_overlays(self, c, ox, oy, pw, ph) -> None:
        self._overlay_photos = []
        ffmpeg = None
        now = float(self.preview_time.get())
        cache = self._overlay_photo_cache
        for i, ov in enumerate(self.custom_overlays[:MAX_CUSTOM_OVERLAYS]):
            if not ov.get("enabled", True) or not overlay_visible_at(ov, now):
                continue
            x = ox + float(ov["x_percent"]) * pw
            y = oy + float(ov["y_percent"]) * ph
            w = max(20, int(float(ov["width_percent"]) * pw))
            path = str(ov.get("path") or "")
            tags = ("editor", "custom_overlay", f"custom_{i}")
            shown = False
            key = (i, path, w)
            photo = cache.get(key)
            if photo is None and path and Path(path).is_file():
                try:
                    if ffmpeg is None:
                        ffmpeg = _tool_path(self.cfg, "ffmpeg")
                    tmp = LOGS_DIR / f"preview_overlay_{i}.png"
                    scale_logo_preview(ffmpeg, path, w, str(tmp))
                    photo = tk.PhotoImage(file=str(tmp))
                    cache[key] = photo
                except Exception:
                    photo = None
            if photo is not None:
                self._overlay_photos.append(photo)
                c.create_image(x, y, image=photo, anchor="nw", tags=tags)
                shown = True
            if not shown:
                c.create_rectangle(x, y, x + w, y + max(16, int(w * 0.25)), fill="#3a2b45",
                                   outline="#ffb3e6", tags=tags)
                c.create_text(x + 4, y + 3, text=Path(path).name[:22] or "missing image",
                              fill="#ffd6f3", anchor="nw", font=("Segoe UI", 8), tags=tags)
            color = "#ffe066" if i == self.selected_overlay else "#ff9ee6"
            c.create_rectangle(x, y, x + w, y + max(16, int(w * 0.25)), outline=color, width=2,
                               dash=(5, 3), tags=tags)
            c.create_text(x + 3, y - 12, text=f"OV{i+1}", fill=color, anchor="nw",
                          font=("Segoe UI", 9, "bold"), tags=tags)

    def _quality_code(self) -> str:
        return next((code for code, label in QUALITY_LABELS.items() if label == self.quality.get()), "1080")

    def _quality_changed(self) -> None:
        self.cfg["video_quality"] = self._quality_code()
        save_config(self.cfg)
        if self._quality_code() != "1080":
            self._log(f"🎞️ Quality {self._quality_code()}p: the video parts are made smaller and faster "
                      "(title / logo / subtitle keep their look); the translation and voice are kept.")

    def _subtitle_changed(self) -> None:
        try:
            self.cfg["subtitles"] = self.subtitle_controls.values()
        except (ValueError, tk.TclError):
            return
        save_config(self.cfg)
        self._redraw_editor_items()

    def _draw_subtitle_preview(self, ox, oy, pw, ph) -> None:
        if not hasattr(self, "subtitle_controls"):
            return
        try:
            values = self.subtitle_controls.values()
        except (ValueError, tk.TclError):
            return
        if not values["enabled"]:
            return
        frame = self._output_canvas()
        target = self._target_code()
        identity = dict(values)
        identity.pop("position")
        key = json.dumps([identity, frame, target], sort_keys=True)
        if key != self._subtitle_preview_key and not self._subtitle_busy:
            self._subtitle_busy = True

            def render():
                path = None
                try:
                    path = subtitle_sample(values, frame, LOGS_DIR / "subtitle_preview", target)
                except Exception as exc:
                    self._log(f"⚠️ Subtitle preview: {exc}")
                self.msgq.put(("subtitle_preview", (key, path)))

            threading.Thread(target=render, daemon=True).start()
        if key != self._subtitle_preview_key or not self._subtitle_preview_path:
            return
        from PIL import Image, ImageTk
        width, height, x, y = subtitle_geometry(values, frame)
        scale = pw / frame[0]
        size = max(1, round(width * scale)), max(1, round(height * scale))
        scaled_key = str(self._subtitle_preview_path), size
        if scaled_key != self._subtitle_scaled_key:
            with Image.open(self._subtitle_preview_path) as img:
                self._subtitle_photo = ImageTk.PhotoImage(img.resize(size, Image.Resampling.LANCZOS))
            self._subtitle_scaled_key = scaled_key
        self.preview_canvas.create_image(ox + x * scale, oy + y * scale, image=self._subtitle_photo,
                                         anchor="nw", tags=("editor", "subtitle"))

    def _preview_press(self, event) -> None:
        x, y = self._preview_xy(event)
        if self._draw_blur_mode:
            self._draw_start = (x, y)
            self._temp_rect = self.preview_canvas.create_rectangle(x, y, x, y, outline="#48d7ff", width=2, dash=(6,4), tags=("editor", "temp_blur"))
            return
        items = self.preview_canvas.find_overlapping(x-3, y-3, x+3, y+3)
        tags = []
        for item in reversed(items):
            t = self.preview_canvas.gettags(item)
            if t:
                tags.append(t)

        # V2 crop box (8 handles) and custom image overlays take priority.
        ox, oy, pw, ph = self._preview_bounds()
        fx, fy = (x - ox) / pw, (y - oy) / ph
        handle = next((z for t in tags for z in t if z.startswith("crop_handle_")), None)
        if self.crop_enabled.get() and handle:
            self._crop_drag = ("handle", int(handle.rsplit("_", 1)[1]), fx, fy, self._crop_values())
            return
        if self.crop_enabled.get() and any("crop_box" in t for t in tags):
            self._crop_drag = ("move", -1, fx, fy, self._crop_values())
            return
        custom = next((z for t in tags for z in t if z.startswith("custom_") and z != "custom_overlay"), None)
        if custom:
            idx = int(custom.rsplit("_", 1)[1])
            self.selected_overlay = idx
            self._drag_kind = "custom_overlay"
            self._drag_index = idx
            self._drag_start = (fx, fy)
            self._drag_original = [float(self.custom_overlays[idx]["x_percent"]),
                                   float(self.custom_overlays[idx]["y_percent"])]
            self._redraw_editor_items()
            return

        self._drag_kind = None
        self._drag_index = None
        self._drag_start = (x, y)
        for t in tags:
            handle = next((z for z in t if z.startswith("blur_handle_")), None)
            blur = next((z for z in t if z.startswith("blur_") and not z.startswith("blur_handle_")), None)
            if handle:
                i = int(handle.rsplit("_",1)[1]); self.selected_blur=i; self._drag_kind="blur_resize"; self._drag_index=i; self._drag_original=dict(self.blur_boxes[i]); break
            if "title" in t:
                self._drag_kind="title"; break
            if "subtitle" in t:
                self._drag_kind="subtitle"; self._drag_original=list(self.subtitle_controls.position); break
            if "extra_text" in t:
                self._drag_kind="extra_text"; break
            if "logo" in t:
                self._drag_kind="logo"; self._drag_original=list(self.logo_pos); break
            if blur:
                i=int(blur.rsplit("_",1)[1]); self.selected_blur=i; self._drag_kind="blur_move"; self._drag_index=i; self._drag_original=dict(self.blur_boxes[i]); break
        self._redraw_editor_items()

    def _preview_double_click(self, event) -> None:
        """Double-click a custom image overlay to replace its file (spec §4.4)."""
        x, y = self._preview_xy(event)
        for item in reversed(self.preview_canvas.find_overlapping(x - 2, y - 2, x + 2, y + 2)):
            for tag in self.preview_canvas.gettags(item):
                if tag.startswith("custom_") and tag != "custom_overlay":
                    try:
                        index = int(tag.rsplit("_", 1)[1])
                    except ValueError:
                        return
                    if 0 <= index < len(self.custom_overlays):
                        self.selected_overlay = index
                        self._browse_custom_overlay(index)
                    return

    def _preview_motion(self, event) -> None:
        x, y = self._preview_xy(event)
        if self._draw_blur_mode and self._draw_start and self._temp_rect:
            self.preview_canvas.coords(self._temp_rect, self._draw_start[0], self._draw_start[1], x, y)
            return
        if self._crop_drag:
            self._apply_crop_drag(x, y)
            return
        if self._drag_kind == "custom_overlay" and self._drag_index is not None and self._drag_start:
            ox, oy, pw, ph = self._preview_bounds()
            sx, sy = self._drag_original
            fx, fy = (x - ox) / pw, (y - oy) / ph
            ov = self.custom_overlays[self._drag_index]
            ov["x_percent"] = self._clampf(sx + fx - self._drag_start[0], 0.0, 0.98)
            ov["y_percent"] = self._clampf(sy + fy - self._drag_start[1], 0.0, 0.98)
            self._redraw_editor_items()
            return
        if not self._drag_kind or not self._drag_start:
            return
        ox, oy, pw, ph = self._preview_bounds()
        dx=(x-self._drag_start[0])/pw; dy=(y-self._drag_start[1])/ph
        if self._drag_kind == "title":
            self.title_pos=[(x-ox)/pw, (y-oy)/ph]
        elif self._drag_kind == "subtitle":
            sx, sy = self._drag_original
            self.subtitle_controls.position = [max(0., min(1., sx + dx)), max(0., min(1., sy + dy))]
        elif self._drag_kind == "extra_text":
            self.extra_text_pos=[(x-ox)/pw, (y-oy)/ph]
        elif self._drag_kind == "logo":
            lx,ly=self._drag_original
            self.logo_pos=[max(0.0,min(0.95,lx+dx)), max(0.0,min(0.95,ly+dy))]
        elif self._drag_index is not None:
            i=self._drag_index; old=self._drag_original; b=dict(old)
            if self._drag_kind == "blur_move":
                b["x"]=max(0.0,min(1.0-old["w"],old["x"]+dx)); b["y"]=max(0.0,min(1.0-old["h"],old["y"]+dy))
            else:
                b["w"]=max(0.015,min(1.0-old["x"],old["w"]+dx)); b["h"]=max(0.015,min(1.0-old["y"],old["h"]+dy))
            self.blur_boxes[i]=b
        self._redraw_editor_items()

    def _preview_release(self, event) -> None:
        x, y = self._preview_xy(event)
        if self._crop_drag:
            self._crop_drag = None
            self._redraw_editor_items()
            return
        if self._draw_blur_mode and self._draw_start:
            ox, oy, pw, ph = self._preview_bounds()
            x1,y1=self._draw_start; x2,y2=x,y
            if abs(x2-x1)>=12 and abs(y2-y1)>=12:
                xa,xb=sorted((x1,x2)); ya,yb=sorted((y1,y2))
                self.blur_boxes.append({"x":(xa-ox)/pw,"y":(ya-oy)/ph,"w":(xb-xa)/pw,"h":(yb-ya)/ph})
                self.selected_blur=len(self.blur_boxes)-1
            self._draw_blur_mode=False; self._draw_start=None; self._temp_rect=None
            self.preview_canvas.configure(cursor="arrow")
            self.status.set("READY")
            self._redraw_editor_items()
            return
        if self._drag_kind == "subtitle":
            self.cfg["subtitles"] = self.subtitle_controls.values()
            save_config(self.cfg)
        self._drag_kind=None; self._drag_index=None; self._drag_start=None; self._drag_original=None
        self._redraw_editor_items()

    def _append_log_file(self, text: str) -> None:
        paths = [LOGS_DIR / "latest.log"]
        if self.run_log_path:
            paths.append(self.run_log_path)
        stamp = datetime.now().strftime("%H:%M:%S")
        for path in paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"[{stamp}] {text}\n")
            except Exception:
                pass

    def _log(self, text: str) -> None:
        self.msgq.put(("log", str(text)))

    def _set_progress(self, current: int, total: int, label: str) -> None:
        # Also track the active batch row from the worker thread (spec §10 BQ-03).
        item = self._active_batch_item
        if item is not None and total:
            item.progress = max(0.0, min(100.0, current * 100.0 / total))
        self.msgq.put(("progress", (current, total, label)))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    text = str(payload)
                    self.logbox.insert("end", text + "\n")
                    self.logbox.see("end")
                    self._append_log_file(text)
                elif kind == "progress":
                    current, total, label = payload
                    pct = 0 if not total else current * 100.0 / total
                    self.progress["value"] = max(0, min(100, pct))
                    # Per-video progress for the batch tree (spec §10 BQ-03).
                    if self._active_batch_item is not None:
                        self._active_batch_item.progress = max(0.0, min(100.0, pct))
                        self._update_batch_row(self._active_batch_item)
                    if not self._progress_started_at:
                        self._progress_started_at = time.monotonic()
                    eta = self._update_progress_eta(int(current), int(total),
                                                   time.monotonic() - self._progress_started_at)
                    self.status.set(f"{label} | {eta}" if int(total) > 1 else str(label))
                elif kind == "stage":
                    self._progress_started_at = time.monotonic()
                    self.stage.set(str(payload))
                elif kind == "batch_refresh":
                    self._refresh_batch_view()
                elif kind == "crop_detected":
                    self._apply_crop_detection(payload)
                elif kind == "subtitle_preview":
                    key, path = payload
                    self._subtitle_busy = False
                    self._subtitle_preview_key, self._subtitle_preview_path = key, path
                    self._redraw_editor_items()
                elif kind == "job":
                    self.job_display.set(str(payload))
                elif kind == "done":
                    self._running(False)
                    self.progress["value"] = 100
                    self.status.set("DONE")
                    self.stage.set("Completed")
                    messagebox.showinfo("DONE", str(payload))
                elif kind == "error":
                    self._running(False)
                    self.status.set("ERROR")
                    self.stage.set("Stopped")
                    hint = self.suggest_for_error(str(payload))
                    self._log(f"💡 Suggested fix: {hint}")
                    messagebox.showerror("ERROR", f"{payload}\n\n💡 Suggested fix:\n{hint}")
                    failed_segment = failed_segment_from_error(str(payload))
                    if failed_segment and messagebox.askyesno(
                        "Edit Text",
                        f"Line {failed_segment} မှာ အသံထုတ်လို့ မရလို့ ရပ်သွားပါတယ်။\n"
                        "စာသားကြောင့် ဖြစ်နိုင်ပါတယ် (ဥပမာ English စာလုံးတွေ ရောနေတာ) — အဲ့စာကြောင်းကို ဖွင့်ပြီး ပြင်မလား?\n"
                        "ပြင်ပြီး Save နှိပ်ပြီးရင် START / RESUME ပြန်နှိပ်ပါ။",
                    ):
                        self._open_text_editor(failed_segment)
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _browse_final_folder(self) -> None:
        folder = filedialog.askdirectory(title="Save the finished videos in")
        if folder:
            self.final_folder.set(folder)

    def _source_language_code(self) -> str:
        return next((code for code, label in LANGUAGE_LABELS.items() if label == self.source_language.get()), "auto")

    def _target_code(self) -> str:
        return next((code for code, label in TARGET_LABELS.items() if label == self.target_language.get()), "mm")

    def _narrator_voices(self) -> tuple[str, ...]:
        return NARRATOR_VOICES_EN if self._target_code() == "en" else NARRATOR_VOICES

    def _target_hint_text(self) -> str:
        return ("English: rewrite the English wording (not translate) · en-GB-Ryan = British narrator"
                if self._target_code() == "en" else
                "Myanmar = translate  ·  English = rewrite the English wording, same meaning")

    def _narrator_voice_hint(self) -> str:
        return ("en-GB-Ryan = English UK · en-US/en-AU/... = other English accents · gemini: = Gemini TTS"
                if self._target_code() == "en" else
                "my-MM = Edge TTS · gemini: = Gemini TTS (.env keys) — one voice all through")

    def _set_target(self, code: str) -> None:
        if code not in TARGET_LABELS:
            code = "mm"
        self.target_language.set(TARGET_LABELS[code])
        self._target_changed()

    def _target_changed(self) -> None:
        """Switch the voice list and subtitle font list between Myanmar and English output."""
        code = self._target_code()
        voices = self._narrator_voices()
        if hasattr(self, "edge_voice_combo"):
            self.edge_voice_combo.configure(values=voices)
            if self.edge_voice.get() not in voices:
                stored = str(self.cfg.get(f"edge_tts_voice_{code}") or "")
                self.edge_voice.set(stored if stored in voices else voices[0])
        if hasattr(self, "edge_voice_hint"):
            self.edge_voice_hint.configure(text=self._narrator_voice_hint())
        if hasattr(self, "target_hint"):
            self.target_hint.configure(text=self._target_hint_text())
        if hasattr(self, "subtitle_controls"):
            try:
                families = latin_fonts() if code == "en" else list(myanmar_fonts())
                self.subtitle_controls.set_font_choices(families)
            except Exception:
                pass
        self.cfg["target_language"] = code
        save_config(self.cfg)
        self._subtitle_preview_key = None
        self._draw_subtitle_preview(*self._subtitle_preview_origin())
        self._log(f"🌐 Output: {'English (rewrite)' if code == 'en' else 'Myanmar (translate)'}")

    def _subtitle_preview_origin(self) -> tuple[int, int, int, int]:
        try:
            return self._preview_bounds()
        except Exception:
            return 0, 0, PREVIEW_W, PREVIEW_H

    def _browse_env(self) -> None:
        current = self.gemini_env.get().strip()
        initial = str(Path(current).parent) if current else str(DATA_DIR)
        p = filedialog.askopenfilename(title="Select .env containing GEMINI_API_KEY", initialdir=initial, filetypes=[("ENV", ".env"), ("All", "*.*")])
        if p:
            self.gemini_env.set(p)

    def _clear_batch(self) -> None:
        if self.batch_files or self.batch_manager.queue:
            self.batch_files = []
            self.batch_folder = None
            self.batch_manager.queue.clear()
            self._active_batch_item = None
            self._refresh_batch_view()

    def _browse_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select folder of source videos")
        if not folder:
            return
        files = _list_batch_videos(Path(folder))
        if not files:
            messagebox.showinfo("Folder Batch", "ဒီ folder ထဲမှာ video ဖိုင် (mp4/mkv/mov/webm/avi) မတွေ့ပါ။")
            return
        # Load the first video so preview / editor overlays / plan state work as usual.
        self._load_source(str(files[0]))
        self.batch_folder = Path(folder)
        self.batch_manager.queue.clear()
        self.batch_manager.add_files(files)
        self._refresh_batch_view()
        self.job_display.set(f"Batch: {len(files)} video(s) · {Path(folder).name}")
        self._log(f"📂 Folder batch: {len(files)} video(s) in {folder}")
        for i, f in enumerate(files, 1):
            done = " (already done — will skip)" if _job_final_output(_job_dir_for(f)) else ""
            self._log(f"   {i}. {f.name}{done}")
        self._log("ℹ️ Title / Logo / Blur / Crop / Color / Overlay settings on screen are applied to every video in the batch.")

    def _browse_source(self) -> None:
        p = filedialog.askopenfilename(title="Select source video", filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi"), ("All", "*.*")])
        if p:
            self._clear_batch()
            self._load_source(p)

    def _load_source(self, p: str) -> None:
        if p:
            self.source.set(p)
            self.source_title.set(Path(p).stem)
            self.blur_boxes = []
            self.selected_blur = None
            job = _job_dir_for(Path(p))
            # Resume editor layout too, not only transcript/TTS checkpoints.
            editor_file = job / "editor_overlays.json"
            if editor_file.is_file():
                try:
                    ed = json.loads(editor_file.read_text(encoding="utf-8"))
                    self.blur_boxes = [dict(x) for x in (ed.get("blur_boxes") or []) if isinstance(x, dict)]
                    self.title_text.set(str(ed.get("title_text") or ""))
                    self.title_pos = list(ed.get("title_position") or self.title_pos)
                    self.title_size.set(int(ed.get("title_font_size", self.title_size.get())))
                    # V19: legacy editor files had no logo_enabled flag. Do not let those
                    # cached paths silently resurrect a logo the user has disabled globally.
                    job_logo_enabled = bool(ed.get("logo_enabled", self.cfg.get("logo_enabled", False)))
                    self.logo_enabled.set(job_logo_enabled)
                    if job_logo_enabled and str(ed.get("logo_path") or "").strip():
                        self.logo_path.set(str(ed.get("logo_path")))
                    elif not job_logo_enabled:
                        self.logo_path.set("")
                    self.logo_pos = list(ed.get("logo_position") or self.logo_pos)
                    self.logo_width_frac.set(float(ed.get("logo_width_fraction", self.logo_width_frac.get())))
                    self.logo_opacity.set(float(ed.get("logo_opacity", self.logo_opacity.get())))
                    self.extra_text.set(str(ed.get("extra_text") or ""))
                    self.extra_text_pos = list(ed.get("extra_text_position") or self.extra_text_pos)
                    self.extra_text_size.set(int(ed.get("extra_text_font_size", self.extra_text_size.get())))
                    self.extra_text_opacity.set(float(ed.get("extra_text_opacity", self.extra_text_opacity.get())))
                    # V2 job settings (spec §4, §5, §8)
                    if isinstance(ed.get("crop"), dict):
                        self._apply_crop_values(ed["crop"])
                    if isinstance(ed.get("color_correction"), dict):
                        self._apply_color_values(ed["color_correction"])
                    if isinstance(ed.get("audio_ducking"), dict):
                        self._apply_ducking_values(ed["audio_ducking"])
                    if isinstance(ed.get("custom_overlays"), list):
                        self.custom_overlays = [normalize_overlay(o, i + 1) for i, o in
                                                enumerate(ed["custom_overlays"][:MAX_CUSTOM_OVERLAYS])]
                        self._rebuild_overlay_rows()
                except Exception as exc:
                    self._log(f"⚠️ Editor layout resume error: {exc}")
            self.job_display.set(f"Job: {job.name}")
            ready = not missing_artifacts(job) and _job_final_output(job) is not None
            if hasattr(self, "rerender_btn"):
                self.rerender_btn.configure(state="normal" if ready else "disabled")
            if ready:
                self._apply_ratio_profile(self.output_ratio.get(), quiet=True)
            try:
                ffprobe = _tool_path(self.cfg, "ffprobe")
                self.preview_duration = preview_probe_duration(ffprobe, p)
                self.source_size = probe_video_size(ffprobe, p)
                self.preview_slider.configure(to=max(0.1, self.preview_duration))
                t = min(max(0.0, float(self.cfg.get("preview_seek_seconds", 1.0))), max(0.0, self.preview_duration-0.05))
                self.preview_time.set(t)
            except Exception as exc:
                self.preview_duration = 0.0
                self._log(f"⚠️ Preview duration error: {exc}")
            self._refresh_preview()

    def _save_cfg(self, settings: dict) -> None:
        self.cfg["gemini_env_path"] = settings["gemini_env_path"]
        self.cfg["source_language"] = settings["source_language"]
        target = str(settings.get("target_language") or "mm")
        self.cfg["target_language"] = target
        self.cfg["final_output_folder"] = settings["final_output_folder"]
        self.cfg["reuse_transcript_and_plan"] = settings["reuse_transcript_and_plan"]
        self.cfg[f"edge_tts_voice_{target}"] = settings["edge_tts_voice"]
        self.cfg["narrator_voice"] = settings["voice"]
        self.cfg["gemini_direction"] = settings["gemini_direction"]
        self.cfg["subtitles"] = settings["overlays"]["subtitles"]
        self.cfg["edge_tts_rate"] = settings["edge_tts_rate"]
        self.cfg["edge_tts_pitch"] = settings["edge_tts_pitch"]
        # Engine settings stay fixed/hidden; only user-facing production values persist.
        self.cfg["zoom_factor"] = settings["zoom_factor"]
        self.cfg["mirror_video"] = bool(settings["mirror_video"])
        self.cfg["voice_volume_percent"] = int(settings.get("voice_volume_percent", 100))
        self.cfg["short_pauses"] = bool(settings.get("short_pauses", True))
        self.cfg["output_ratio"] = settings["output_ratio"]
        self.cfg["export_two_ratios"] = bool(settings.get("export_two_ratios", self.export_two_ratios.get()))
        self.cfg["second_output_ratio"] = str(settings.get("second_output_ratio") or self.second_output_ratio.get())
        self.cfg["video_quality"] = settings["video_quality"]
        self.cfg["frame_fill"] = settings["frame_fill"]
        self.cfg["logo_enabled"] = bool(settings["overlays"].get("logo_enabled", False))
        self.cfg["logo_path"] = settings["overlays"].get("logo_path", "") if self.cfg["logo_enabled"] else ""
        self.cfg["logo_width_fraction"] = settings["overlays"].get("logo_width_fraction", 0.16)
        self.cfg["logo_opacity"] = settings["overlays"].get("logo_opacity", 0.65)
        self.cfg["logo_position"] = settings["overlays"].get("logo_position", [0.80, 0.06])
        self.cfg["title_position"] = settings["overlays"].get("title_position", [0.50, 0.12])
        self.cfg["title_font_size"] = settings["overlays"].get("title_font_size", 54)
        self.cfg["extra_text_position"] = settings["overlays"].get("extra_text_position", [0.50, 0.84])
        self.cfg["extra_text_font_size"] = settings["overlays"].get("extra_text_font_size", 42)
        self.cfg["extra_text_opacity"] = settings["overlays"].get("extra_text_opacity", 0.65)
        # V2 settings (spec §3-§8) must survive an app restart as well.
        self.cfg["crop"] = settings.get("crop") or self._crop_values()
        self.cfg["color_correction"] = settings.get("color_correction") or self._color_values()
        self.cfg["custom_overlays"] = [dict(o) for o in (settings.get("custom_overlays") or self.custom_overlays)]
        self.cfg["audio_ducking"] = settings.get("audio_ducking") or self._ducking_values()
        self.cfg["tts_engine"] = str(settings.get("tts_engine") or self._tts_engine_code())
        self.cfg["edge_tts_ssml"] = bool(settings.get("edge_tts_ssml", self.edge_tts_ssml.get()))
        self.cfg["preview_seek_seconds"] = float(self.preview_time.get())
        self._save_ratio_profile(settings["output_ratio"], settings)
        save_config(self.cfg)

    def _capture_settings(self) -> dict:
        source = Path(self.source.get().strip()).resolve()
        env_text = str(self.gemini_env.get().strip() or self.cfg.get("gemini_env_path") or "")
        gemini_env = Path(env_text).resolve() if env_text else None
        voice = self.edge_voice.get().strip()
        if not source.is_file():
            raise ValueError("Source video ကိုရွေးပါ။")
        if gemini_env is not None and not gemini_env.is_file():
            raise ValueError("Gemini .env path မမှန်ပါ။ Gemini .env မှာ Browse နှိပ်ပြီး GEMINI_API_KEY ပါတဲ့ .env ဖိုင်ကိုရွေးပါ။")
        target = self._target_code()
        if voice not in self._narrator_voices():
            raise ValueError("Narrator Voice ကိုရွေးပါ။")
        if target == "en" and self._source_language_code() not in ("auto", "en"):
            raise ValueError(
                "English mode rewrites an English video.\n"
                "Choose English in Source language, or switch Output to Myanmar (translate)."
            )
        for tool in ("ffmpeg", "ffprobe"):
            if not Path(_tool_path(self.cfg, tool)).is_file():  # otherwise the job dies with "[WinError 2]"
                raise ValueError(
                    f"{tool}.exe မတွေ့ပါ — FFmpeg မသွင်းရသေးပါ။\n\n"
                    "INSTALL_REQUIREMENTS.bat ကို ပြန် run ပါ။ FFmpeg ကို ဒီ tool ရဲ့ ffmpeg folder ထဲ အလိုလို ထည့်ပေးပါမယ်။\n"
                    "(သို့) https://www.gyan.dev/ffmpeg/builds/ ကနေ download လုပ်ပြီး ffmpeg.exe နဲ့ ffprobe.exe ကို "
                    "RUN.bat ဘေးက ffmpeg folder ထဲ ထည့်ပါ။"
                )
        runtime_root = ROOT_DIR / "recap_runtime"
        for rel in ("services/whisper_service.py", "services/ai_service.py"):
            if not (runtime_root / rel).is_file():
                raise ValueError(f"Bundled Recap runtime file missing: {rel}")
        zoom = float(self.zoom_factor.get())
        if not (1.0 <= zoom <= 1.25):
            raise ValueError("Zoom must be between 1.00 and 1.25")
        self._commit_overlay_times()
        logo_enabled = bool(self.logo_enabled.get())
        logo_path = self.logo_path.get().strip() if logo_enabled else ""
        target = self._target_code()
        latin_font = _latin_title_font() if target == "en" else ""
        title_font = latin_font or str(self.cfg.get("title_font_path", r"C:\Windows\Fonts\mmrtext.ttf"))
        overlays = {
            "subtitles": self.subtitle_controls.values(),
            "blur_boxes": [dict(x) for x in self.blur_boxes],
            "blur_strength": int(self.cfg.get("overlay_blur_strength", 18)),
            "title_text": self.title_text.get().strip(),
            "title_position": [float(self.title_pos[0]), float(self.title_pos[1])],
            "title_font_size": int(self.title_size.get()),
            "title_font_path": title_font,
            "title_use_source_font": bool(latin_font),
            "logo_enabled": logo_enabled,
            "logo_path": logo_path,
            "logo_position": [float(self.logo_pos[0]), float(self.logo_pos[1])],
            "logo_width_fraction": float(self.logo_width_frac.get()),
            "logo_opacity": float(self.logo_opacity.get()),
            "extra_text": self.extra_text.get().strip(),
            "extra_text_position": [float(self.extra_text_pos[0]), float(self.extra_text_pos[1])],
            "extra_text_font_size": int(self.extra_text_size.get()),
            "extra_text_opacity": float(self.extra_text_opacity.get()),
            # Persisted with the job editor layout too (spec §4 / §12).
            "custom_overlays": [dict(o) for o in self.custom_overlays],
        }
        if overlays["logo_path"] and not Path(overlays["logo_path"]).is_file():
            raise ValueError("Logo image path မမှန်ပါ။")
        final_folder = self.final_folder.get().strip()
        if final_folder:
            try:
                Path(final_folder).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"Save final to: folder ကို မဖန်တီးနိုင်ပါ ({exc})")
        tts_engine = self._tts_engine_code()
        if target == "en" and tts_engine in ("mms_myanmar", "piper"):
            tts_engine = "edge_tts"          # local engines are Burmese-only; English uses Edge / Gemini
        return {
            "source": source,
            "source_language": self._source_language_code(),
            "target_language": target,
            "final_output_folder": final_folder,
            "source_title": self.source_title.get().strip() or source.stem,
            "edge_tts_voice": voice,
            "edge_tts_rate": _format_edge_rate(self.edge_rate_percent.get()),
            "edge_tts_pitch": _format_edge_pitch(self.edge_pitch_hz.get()),
            "gemini_env_path": str(gemini_env) if gemini_env is not None else "",
            "voice": voice,
            "gemini_direction": self.gemini_direction.get().strip(),
            # V0.3 behavior, hidden from UI.
            "max_video_speed": float(self.cfg.get("max_video_speed", 1.25)),
            "max_video_slow": float(self.cfg.get("max_video_slow", 1.35)),
            "smooth_freeze_fallback": bool(self.cfg.get("smooth_freeze_fallback", False)),
            "max_freeze_hold": float(self.cfg.get("max_freeze_hold", 0.75)),
            "mirror_video": bool(self.mirror_video.get()),
            "voice_volume_percent": int(self.voice_volume.get()),
            "short_pauses": bool(self.short_pauses.get()),
            "output_ratio": self.output_ratio.get() if self.output_ratio.get() in OUTPUT_RATIOS else "16:9",
            "export_two_ratios": bool(self.export_two_ratios.get()),
            "second_output_ratio": self._ratio_label(self.second_output_ratio.get()),
            "video_quality": self._quality_code(),
            "frame_fill": self._fill_code() if self._fill_code() in FRAME_FILLS else "blur",
            "zoom_factor": zoom,
            "render_final_video": True,
            "reuse_transcript_and_plan": bool(self.cfg.get("reuse_transcript_and_plan", self.cfg.get("reuse_whisper_and_plan", True))),
            "overlays": overlays,
            # V2 settings (spec §3-§8): these flow into _render_worker.json.
            "crop": self._crop_values(),
            "color_correction": self._color_values(),
            "custom_overlays": [dict(o) for o in self.custom_overlays],
            "audio_ducking": self._ducking_values(),
            "tts_engine": tts_engine,
            "edge_tts_ssml": bool(self.edge_tts_ssml.get()),
        }

    def _write_worker_cfg(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _run_child(self, script_name: str, cfg_path: Path, stage_name: str) -> dict:
        self.msgq.put(("stage", stage_name))
        self._log(f"\n{'=' * 64}\n{stage_name}\n{'=' * 64}")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if is_frozen():
            # Packaged app: relaunch the same .exe in worker mode instead of
            # running a .py file, which does not exist inside the bundle.
            cmd = [sys.executable, "--run-worker", script_name, str(cfg_path)]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            cmd = [sys.executable, "-u", str(ROOT_DIR / "app" / script_name), str(cfg_path)]
            creationflags = 0
        self._log("▶ " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
        with self.proc_lock:
            self.current_proc = proc
        result: dict = {}
        reported_error = ""
        stopped_message = ""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                    except Exception:
                        self._log(line)
                        continue
                    kind = event.get("kind")
                    if kind == "log":
                        self._log(str(event.get("text", "")))
                    elif kind == "progress":
                        self._set_progress(int(event.get("current", 0)), int(event.get("total", 0)), str(event.get("label", stage_name)))
                    elif kind == "result":
                        result = event
                    elif kind == "error":
                        reported_error = str(event.get("message", ""))
                    elif kind == "stopped":
                        stopped_message = str(event.get("message", "Stopped"))
                    continue
                self._log(line)
            code = proc.wait()
        finally:
            with self.proc_lock:
                if self.current_proc is proc:
                    self.current_proc = None

        if code == 10 or stopped_message:
            raise InterruptedError(stopped_message or "Stopped safely")
        if code != 0:
            if reported_error:
                raise RuntimeError(reported_error + f"\n\nChild exit code: {code}\nLog: {self.run_log_path}")
            raise RuntimeError(_native_crash_message(code) + f"\n\nLog: {self.run_log_path}")
        return result

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if getattr(self, "editing_second_ratio", False):
            self._done_second_style()      # save the 2nd style and restore the main Ratio
        try:
            settings = self._capture_settings()
            if settings.get("export_two_ratios") and settings.get("second_output_ratio") == settings.get("output_ratio"):
                raise ValueError("Two-ratio mode needs a second Ratio different from the main Ratio.")
            self._save_cfg(settings)
        except Exception as exc:
            messagebox.showerror("Setup Error", str(exc))
            return

        batch = [Path(p) for p in self.batch_files]
        sources: list[Path] = batch or [settings["source"]]
        batch_items = {item.path: item for item in self.batch_manager.queue}

        self.stop_event.clear()
        self._running(True)
        self.logbox.delete("1.0", "end")
        self.progress["value"] = 0
        self._progress_started_at = time.monotonic()
        self.status.set("STARTING")
        self.stage.set("Preparing")
        output_label = "English (rewrite)" if settings.get("target_language") == "en" else "Myanmar (translate)"
        self._log(f"📦 {self.source_language.get()} → {output_label} · Narrator only · Edge TTS · V2 Smart Sync")
        if settings.get("export_two_ratios"):
            self._log(f"🎬 Mode: TWO ratios — {settings['output_ratio']} first, "
                      f"{settings['second_output_ratio']} second (script + voice reused, no extra API/TTS).")
        else:
            self._log(f"🎬 Mode: ONE ratio — {settings['output_ratio']}.")
        self._log(f"📁 App root: {ROOT_DIR}")
        self._log(f"📁 User data: {DATA_DIR}")
        if batch:
            self._log(f"📂 Folder batch: {len(batch)} video(s) — one at a time")

        # Capture every Tk variable on the GUI thread; worker thread uses plain data only.
        ffmpeg = _tool_path(self.cfg, "ffmpeg")
        ffprobe = _tool_path(self.cfg, "ffprobe")
        cfg_values = {
            "translation_batch_max_items": int(self.cfg.get("translation_batch_max_items", 40)),
            "story_context_batch_max_items": int(self.cfg.get("story_context_batch_max_items", 80)),
            "context_overlap": int(self.cfg.get("context_overlap", 6)),
            "lookahead": int(self.cfg.get("lookahead", 6)),
            "translation_audit": bool(self.cfg.get("translation_audit", True)),
            "atomic_max_duration": float(self.cfg.get("atomic_max_duration", 5.6)),
            "atomic_target_duration": float(self.cfg.get("atomic_target_duration", 4.2)),
            "gemini_request_timeout_seconds": int(self.cfg.get("gemini_request_timeout_seconds", 120)),
            "gemini_text_model": str(self.cfg.get("gemini_text_model", "gemini-3.5-flash")),
            "whisper_model_other": str(self.cfg.get("whisper_model_other", "medium")),
        }

        def worker() -> None:
            if not batch:
                self._active_batch_item = None
                try:
                    final, job_dir = self._run_one_job(settings, sources[0], ffmpeg, ffprobe, cfg_values)
                    if settings.get("export_two_ratios"):
                        second_settings, second_ratio = self._prepare_second_ratio(settings, job_dir)
                        second_final, _ = self._run_one_job(second_settings, sources[0], ffmpeg, ffprobe,
                                                            cfg_values, render_only=True)
                        message = (f"ပြီးပါပြီ။\n\n"
                                   f"Main ({settings['output_ratio']}):\n{final}\n\n"
                                   f"Second ({second_ratio}):\n{second_final}\n\n"
                                   f"Job folder:\n{job_dir}\n\n"
                                   "The second ratio reused the script and voice — no extra API or TTS call.")
                    else:
                        message = f"ပြီးပါပြီ။\n\nOutput:\n{final}\n\nJob folder:\n{job_dir}"
                    self.msgq.put(("done", message))
                except InterruptedError as exc:
                    self._log(str(exc))
                    self.msgq.put(("error", str(exc)))
                except Exception as exc:
                    self._log(traceback.format_exc())
                    self.msgq.put(("error", str(exc)))
                return

            ok: list[str] = []
            skipped: list[str] = []
            failed: list[str] = []
            stopped = False
            for index, source in enumerate(sources, 1):
                if self.stop_event.is_set():
                    stopped = True
                    break
                label = f"[BATCH {index}/{len(sources)}] {source.name}"
                self.msgq.put(("job", f"Batch {index}/{len(sources)} · {source.name}"))
                item = batch_items.get(source)
                self._active_batch_item = item
                if item is not None:
                    item.start()
                    self.msgq.put(("batch_refresh", None))
                if not source.is_file():
                    failed.append(f"{source.name} · file missing")
                    self._log(f"❌ {label} — file missing, skipped")
                    if item is not None:
                        self.batch_manager.mark_failed(item, "file missing")
                    self._active_batch_item = None
                    self.msgq.put(("batch_refresh", None))
                    continue
                existing = _job_final_output(_job_dir_for(source))
                if existing is not None and self.batch_manager.skip_completed:
                    skipped.append(source.name)
                    if item is not None:
                        item.skip("already completed")
                        item.output_path = Path(existing)
                        item.progress = 100.0
                    self._log(f"⏭️ {label} — already done ({existing.name}); job folder ဖျက်ရင် ပြန်လုပ်ပါမယ်")
                    self._active_batch_item = None
                    self.msgq.put(("batch_refresh", None))
                    continue
                self._log(f"\n{'#' * 64}\n▶ {label}\n{'#' * 64}")
                one = dict(settings)
                one["source"] = source
                one["source_title"] = source.stem
                try:
                    final, _job_dir = self._run_one_job(one, source, ffmpeg, ffprobe, cfg_values, batch_mode=True)
                    extra = ""
                    if one.get("export_two_ratios"):
                        second_settings, second_ratio = self._prepare_second_ratio(one, _job_dir)
                        second_final, _ = self._run_one_job(second_settings, source, ffmpeg, ffprobe, cfg_values,
                                                            batch_mode=True, render_only=True)
                        extra = f" · {second_ratio}: {second_final}"
                    ok.append(source.name)
                    if item is not None:
                        self.batch_manager.mark_done(item, Path(final))
                    self._log(f"✅ {label} COMPLETE → {final}{extra}")
                except InterruptedError as exc:
                    self._log(str(exc))
                    stopped = True
                    failed.append(f"{source.name} · stopped")
                    if item is not None:
                        self.batch_manager.mark_failed(item, "stopped")
                    self._active_batch_item = None
                    self.msgq.put(("batch_refresh", None))
                    break
                except Exception as exc:
                    self._log(traceback.format_exc())
                    first_line = (str(exc).splitlines() or [type(exc).__name__])[0]
                    failed.append(f"{source.name} · {first_line[:160]}")
                    if item is not None:
                        self.batch_manager.mark_failed(item, first_line)
                    self._log(f"❌ {label} FAILED — continuing with next video")
                self._active_batch_item = None
                self.msgq.put(("batch_refresh", None))

            counts = self.batch_manager.get_summary()
            lines = [
                f"Folder batch {'STOPPED' if stopped else 'DONE'}",
                f"OK={len(ok)} · SKIPPED={len(skipped)} · FAILED={len(failed)} / {len(sources)}",
                f"Queue: ✅ {counts['done']} done · ❌ {counts['failed']} failed · "
                f"⏭️ {counts['skipped']} skipped · ⏳ {counts['pending']} pending",
            ]
            if failed:
                lines.append("")
                lines.append("Failed:")
                lines.extend(f"• {x}" for x in failed)
            summary = "\n".join(lines)
            self._log("\n" + summary)
            self.msgq.put(("job", f"Batch: {len(sources)} video(s) · OK {len(ok)} / FAILED {len(failed)}"))
            self.msgq.put(("batch_refresh", None))
            self.msgq.put(("error" if stopped else "done", summary))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _render_another_ratio(self) -> None:
        """Render the current job again in another ratio, reusing plan + voice."""
        if self.worker and self.worker.is_alive():
            return
        try:
            source = Path(self.source.get().strip()).resolve()
            if not source.is_file():
                raise ValueError("Source video ကိုရွေးပါ။")
            job_dir = _job_dir_for(source)
            missing = missing_artifacts(job_dir)
            if missing:
                raise ValueError(
                    "This job has no finished script / voice yet.\n"
                    "Run START / RESUME once, then use Render This Ratio.\n\nMissing: "
                    + ", ".join(missing)
                )
            if _job_final_output(job_dir) is None:
                raise ValueError("Run START / RESUME once before rendering another ratio.")
            settings = self._capture_settings()
            # The saved voice settings win: a changed voice/rate/engine would force
            # new TTS, which this path is explicitly not allowed to do.
            settings = restore_voice_config(job_dir, settings)
            self._save_cfg(settings)
        except Exception as exc:
            messagebox.showerror("Render This Ratio", str(exc))
            return

        self.stop_event.clear()
        self._running(True)
        self.logbox.delete("1.0", "end")
        self.progress["value"] = 0
        self._progress_started_at = time.monotonic()
        self.status.set("RENDERING RATIO")
        self.stage.set("Reusing script + voice")
        ratio = str(settings.get("output_ratio") or "16:9")
        self._log(f"🔁 RENDER THIS RATIO: {ratio} — script, timeline and voice are already saved.")
        self._log("   Planner / Gemini / Whisper: SKIPPED · Edge TTS: SKIPPED · only video composition runs.")
        ffmpeg = _tool_path(self.cfg, "ffmpeg")
        ffprobe = _tool_path(self.cfg, "ffprobe")
        cfg_values = {
            "translation_batch_max_items": int(self.cfg.get("translation_batch_max_items", 40)),
            "story_context_batch_max_items": int(self.cfg.get("story_context_batch_max_items", 80)),
            "context_overlap": int(self.cfg.get("context_overlap", 6)),
            "lookahead": int(self.cfg.get("lookahead", 6)),
            "translation_audit": bool(self.cfg.get("translation_audit", True)),
            "atomic_max_duration": float(self.cfg.get("atomic_max_duration", 5.6)),
            "atomic_target_duration": float(self.cfg.get("atomic_target_duration", 4.2)),
            "gemini_request_timeout_seconds": int(self.cfg.get("gemini_request_timeout_seconds", 120)),
            "gemini_text_model": str(self.cfg.get("gemini_text_model", "gemini-3.5-flash")),
            "whisper_model_other": str(self.cfg.get("whisper_model_other", "medium")),
        }

        def worker() -> None:
            try:
                final, used_job = self._run_one_job(settings, source, ffmpeg, ffprobe, cfg_values,
                                                    render_only=True)
                self.msgq.put(("done", f"Second ratio ready ({ratio}).\n\nOutput:\n{final}\n\n"
                                       f"Job folder:\n{used_job}\n\n"
                                       "The script, timeline and voice were reused — no API or TTS call."))
            except InterruptedError as exc:
                self._log(str(exc))
                self.msgq.put(("error", str(exc)))
            except Exception as exc:
                self._log(traceback.format_exc())
                self.msgq.put(("error", str(exc)))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _prepare_second_ratio(self, settings: dict, job_dir: Path) -> tuple[dict, str]:
        """Second-ratio settings: saved per-ratio text sizes + the original voice."""
        second, ratio = prepare_second_ratio(settings, job_dir, self.cfg.get("ratio_profiles") or {})
        self._log(f"🎚️ Second ratio {ratio}: text sizes / positions restored from its saved profile.")
        return second, ratio

    def _run_one_job(self, settings: dict, source: Path, ffmpeg: str, ffprobe: str,
                     cfg_values: dict, batch_mode: bool = False, render_only: bool = False) -> tuple[str, Path]:
        """Planner + Edge TTS/Smart Sync for one video. Runs on the worker thread (no Tk calls).

        ``render_only`` skips the planner child entirely and forbids the render
        worker from synthesizing voice again: it only recomposes the saved
        timeline for a different ratio / text size.
        """
        job_dir = _job_dir_for(source)
        job_dir.mkdir(parents=True, exist_ok=True)
        self.current_job = job_dir
        self.run_log_path = job_dir / "run.log"
        try:
            self.run_log_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        stop_file = job_dir / ".stop_requested"
        if not self.stop_event.is_set():
            stop_file.unlink(missing_ok=True)
        self.msgq.put(("job", f"Batch · {job_dir.name}" if batch_mode else f"Job: {job_dir.name}"))
        try:
            # overlays for the production pass plus the V2 job settings that the
            # editor restores when this source is opened again (§4/§5/§6/§8).
            editor_data = dict(settings["overlays"])
            editor_data["crop"] = settings.get("crop") or {}
            editor_data["color_correction"] = settings.get("color_correction") or {}
            editor_data["audio_ducking"] = settings.get("audio_ducking") or {}
            editor_data["custom_overlays"] = settings.get("custom_overlays") or []
            (job_dir / "editor_overlays.json").write_text(
                json.dumps(editor_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._log(f"📁 Job folder: {job_dir}")

        if render_only:
            plan_path = str(job_dir / "smart_sync_plan.json")
            self._log("⏭️ STEP 1 SKIPPED — reusing the saved script, timeline and API/Whisper data.")
        else:
            planner_cfg = job_dir / "_planner_worker.json"
            planner_data = {
                "source_path": str(source),
                "source_title": settings["source_title"],
                "job_dir": str(job_dir),
                "runtime_root": str(ROOT_DIR / "recap_runtime"),
                "gemini_env_path": settings["gemini_env_path"],
                "ffprobe_path": ffprobe,
                "reuse": settings["reuse_transcript_and_plan"],
                "stop_file": str(stop_file),
                "source_language": settings.get("source_language", "auto"),
                "target_language": settings.get("target_language", "mm"),
                "narrator_voice": settings.get("voice", ""),
                "whisper_model_other": cfg_values.get("whisper_model_other", "medium"),
            }
            for key in (
                "translation_batch_max_items", "story_context_batch_max_items", "context_overlap",
                "lookahead", "translation_audit", "atomic_max_duration", "atomic_target_duration",
                "gemini_request_timeout_seconds", "gemini_text_model",
            ):
                planner_data[key] = cfg_values[key]
            self._write_worker_cfg(planner_cfg, planner_data)
            planner_result = self._run_child(
                "planner_worker.py", planner_cfg,
                "STEP 1 — Transcript + Context + Timestamp Translation",
            )
            plan_path = str(planner_result.get("plan_path") or (job_dir / "smart_sync_plan.json"))

        plan_data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        segment_count = len(normalize_segments(plan_data))
        self._log(f"🧩 Final plan: {segment_count} segments → "
                  + ("saved voice (no TTS)" if render_only else "Edge TTS"))

        if stop_file.exists():
            raise InterruptedError("Stopped after planner stage. START / RESUME ပြန်နှိပ်လို့ရပါတယ်။")

        if render_only:
            self._log("✅ Saved script / timeline ready — Gemini / Whisper were not called.")
            self._log(f"♻️ Reusing narration audio — voice={settings['voice']} | "
                      f"output={'English (rewrite)' if settings.get('target_language') == 'en' else 'Myanmar'}")
        else:
            self._log("✅ Transcript/translation complete.")
            self._log(f"🎙️ Narrator TTS — voice={settings['voice']} | "
                      f"output={'English (rewrite)' if settings.get('target_language') == 'en' else 'Myanmar'}")
        time.sleep(0.2)

        render_cfg = job_dir / "_render_worker.json"
        self._write_worker_cfg(render_cfg, {
            "job_dir": str(job_dir),
            "source_path": str(source),
            "plan_path": plan_path,
            "voice": settings["voice"],
            "edge_tts_rate": settings["edge_tts_rate"],
            "edge_tts_pitch": settings["edge_tts_pitch"],
            "ffmpeg_path": ffmpeg,
            "ffprobe_path": ffprobe,
            "max_video_speed": settings["max_video_speed"],
            "max_video_slow": settings["max_video_slow"],
            "smooth_freeze_fallback": settings["smooth_freeze_fallback"],
            "max_freeze_hold": settings["max_freeze_hold"],
            "mirror_video": settings["mirror_video"],
            "zoom_factor": settings["zoom_factor"],
            "render_final_video": settings["render_final_video"],
            "render_only": bool(render_only),
            "overlays": settings["overlays"],
            "voice_volume_percent": settings["voice_volume_percent"],
            "short_pauses": bool(settings.get("short_pauses", True)),
            "output_ratio": settings.get("output_ratio", "16:9"),
            "video_quality": settings.get("video_quality", "1080"),
            "frame_fill": settings.get("frame_fill", "blur"),
            "gemini_env_path": settings.get("gemini_env_path", ""),
            "gemini_direction": settings.get("gemini_direction", ""),
            "stop_file": str(stop_file),
            # V2 (spec §3-§8): crop, color correction, custom overlays, audio
            # ducking and the selected TTS engine.
            "crop": settings.get("crop") or {},
            "color_correction": settings.get("color_correction") or {},
            "custom_overlays": settings.get("custom_overlays") or [],
            "audio_ducking": settings.get("audio_ducking") or {},
            "tts_engine": settings.get("tts_engine", "edge_tts"),
            "edge_tts_ssml": bool(settings.get("edge_tts_ssml", False)),
            "target_language": settings.get("target_language", "mm"),
        })
        stage = ("STEP 2 — Video recompose only (saved voice + timeline)"
                 if render_only else "STEP 2 — Narrator TTS + Smart Sync")
        result = self._run_child("render_worker.py", render_cfg, stage).get("result") or {}
        final = result.get("final") or result.get("audio") or str(job_dir)
        folder = str(settings.get("final_output_folder") or "").strip()
        suffix = "English" if settings.get("target_language") == "en" else "Myanmar"
        if folder and Path(final).is_file():
            name = final_output_filename(
                source.stem, settings.get("output_ratio", "16:9"), suffix,
                two_ratios=bool(settings.get("export_two_ratios")), render_only=bool(render_only),
            )
            target = Path(folder) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final, target)
            self._log(f"📤 Final video saved to {target}")
            srt = Path(str(result.get("srt") or ""))
            if srt.is_file():          # in a folder of its own: players would show a same-name .srt twice
                srt_target = target.parent / "SRT_for_upload" / f"{target.stem}.srt"
                srt_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srt, srt_target)
                self._log(f"📝 SRT saved to {srt_target}")
            final = str(target)
        try:
            variant = record_variant(job_dir, settings, Path(final), source.stem, suffix)
            self._log(f"🗂️ Ratio {settings.get('output_ratio', '16:9')} saved as {variant}")
        except Exception as exc:
            self._log(f"⚠️ Ratio variant metadata not saved: {exc}")
        return str(final), job_dir

    def _stop(self) -> None:
        self.stop_event.set()
        if self.current_job:
            try:
                (self.current_job / ".stop_requested").write_text("stop", encoding="utf-8")
            except Exception:
                pass
        self.status.set("STOP REQUESTED")
        self._log("⏹️ Stop requested — current safe boundary ရောက်တာနဲ့ရပ်မယ်။ Resume data မဖျက်ပါ။")

    def _running(self, yes: bool) -> None:
        self.start_btn.configure(state="disabled" if yes else "normal")
        self.stop_btn.configure(state="normal" if yes else "disabled")
        ready = False
        if not yes and self.current_job is not None:
            ready = not missing_artifacts(self.current_job) and _job_final_output(self.current_job) is not None
        if hasattr(self, "rerender_btn"):
            self.rerender_btn.configure(state="normal" if ready else "disabled")
        if hasattr(self, "style_btn"):
            if yes:
                self.style_btn.configure(state="disabled")
            else:
                self._sync_second_ratio_state()

    # ==================================================================== V2
    # Presets, viral formats, crop, color, custom overlays, ducking, batch,
    # TTS engines, drag-and-drop, ETA and error suggestions (spec §3-§10).
    # ====================================================================
    @staticmethod
    def _clampf(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, float(value)))

    def _safe_label(self, label, text: str) -> None:
        try:
            label.configure(text=str(text))
        except Exception:
            pass

    def _fill_label(self, code: str) -> str:
        return FILL_LABELS.get(str(code), FILL_LABELS["blur"])

    def _quality_label(self, code: str) -> str:
        return QUALITY_LABELS.get(str(code), QUALITY_LABELS["1080"])

    # ---- Presets (§3) -----------------------------------------------------
    def _refresh_preset_combo(self) -> None:
        names = self.preset_manager.get_preset_names()
        self.preset_combo.configure(values=names)
        current = self.preset_name.get()
        if current not in names:
            default = self.preset_manager.get_default_name()
            self.preset_name.set(default or (names[0] if names else ""))

    def _on_preset_selected(self, _event=None) -> None:
        name = self.preset_name.get().strip()
        if name:
            self._load_preset(name)

    def _load_preset(self, name: str, quiet: bool = False) -> None:
        settings = self.preset_manager.get_settings(name)
        if not settings:
            if not quiet:
                messagebox.showwarning("Preset", f"Preset '{name}' was not found.")
            return
        self._pending_preset = dict(settings)
        warnings = self.preset_manager.apply_to_controller(self)
        self.preset_name.set(name)
        self._rebuild_overlay_rows()
        self._sync_preset_ui()
        self._color_changed()
        self._crop_slider_changed()
        self._refresh_preview()
        for warning in warnings:
            self._log(f"⚠️ Preset: {warning}")
        if not quiet:
            self._log(f"📋 Preset loaded: {name}")
            self.status.set(f"PRESET: {name}")

    def _save_preset(self) -> None:
        name = simpledialog.askstring("Save Preset", "Preset name:", initialvalue=self.preset_name.get(), parent=self)
        if name is None:
            return
        clean = " ".join(str(name).split()).strip()[:50]
        if not clean:
            return
        existing = self.preset_manager.find_preset(clean)
        if existing:
            if self.preset_manager.is_factory(existing):
                messagebox.showerror("Preset", f"'{existing}' is a built-in preset — choose another name.")
                return
            if not messagebox.askyesno("Preset", f"Overwrite existing preset '{existing}'?"):
                return
        try:
            settings = self.preset_manager.capture_from_controller(self)
            stored = self.preset_manager.save_preset(clean, settings)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preset", str(exc))
            return
        self._refresh_preset_combo()
        self.preset_name.set(stored)
        self._log(f"💾 Preset saved: {stored}")
        self.status.set(f"PRESET SAVED: {stored}")

    def _delete_preset(self) -> None:
        name = self.preset_name.get().strip()
        if not name:
            return
        if self.preset_manager.is_factory(name):
            messagebox.showinfo("Preset", "Built-in factory presets cannot be deleted.")
            return
        if not messagebox.askyesno("Delete Preset", f"Delete preset '{name}'?"):
            return
        if self.preset_manager.delete_preset(name):
            self._refresh_preset_combo()
            self._log(f"🗑️ Preset deleted: {name}")

    def _set_default_preset(self) -> None:
        name = self.preset_name.get().strip()
        if not name:
            return
        self.preset_manager.set_default(name)
        self._log(f"⭐ Default preset: {name} (auto-loads on app start)")
        self.status.set(f"DEFAULT: {name}")

    def _sync_preset_ui(self) -> None:
        """Refresh labels/sliders a preset changed behind Tk's back (spec §3.6)."""
        try:
            if hasattr(self, "_voice_volume_scale_var"):
                self._voice_volume_scale_var.set(float(self.voice_volume.get()))
                self._voice_volume_changed(self._voice_volume_scale_var.get())
            if hasattr(self, "edge_speed_value"):
                self.edge_speed_value.configure(text=_format_edge_rate(self.edge_rate_percent.get()))
            if hasattr(self, "edge_pitch_value"):
                self._safe_label(self.edge_pitch_value, _format_edge_pitch(self.edge_pitch_hz.get()))
            if hasattr(self, "zoom_value_label"):
                self.zoom_value_label.configure(text=f"{float(self.zoom_factor.get()):.2f}x")
            if hasattr(self, "title_size_label"):
                self.title_size_label.configure(text=str(int(self.title_size.get())))
            if hasattr(self, "extra_size_label"):
                self.extra_size_label.configure(text=str(int(self.extra_text_size.get())))
            if hasattr(self, "logo_size_label"):
                self.logo_size_label.configure(text=f"{float(self.logo_width_frac.get()) * 100:.0f}%")
            if hasattr(self, "logo_opacity_label"):
                self.logo_opacity_label.configure(text=f"{float(self.logo_opacity.get()) * 100:.0f}%")
            if hasattr(self, "extra_opacity_label"):
                self.extra_opacity_label.configure(text=f"{float(self.extra_text_opacity.get()) * 100:.0f}%")
            if hasattr(self, "duck_value_label"):
                self.duck_value_label.configure(text=f"{int(self.duck_volume_percent.get())}%")
            if hasattr(self, "engine_status"):
                self._engine_changed()
        except Exception as exc:  # noqa: BLE001 — a missing optional widget must never break preset load
            self._log(f"⚠️ Preset UI refresh skipped: {exc}")

    # ---- Viral format buttons (§9.2) ---------------------------------------
    def _apply_viral_format(self, label: str) -> None:
        fmt = next((f for name, f in VIRAL_FORMATS if name == label), None)
        if not fmt:
            return
        self.output_ratio.set(fmt["output_ratio"])
        self.frame_fill.set(self._fill_label(fmt["frame_fill"]))
        self.quality.set(self._quality_label(fmt["video_quality"]))
        self.zoom_factor.set(float(fmt["zoom_factor"]))
        self.mirror_video.set(bool(fmt["mirror_video"]))
        self._zoom_changed(self.zoom_factor.get())
        self._save_ratio_profile(self.output_ratio.get())
        self._refresh_preview()
        self.status.set(f"FORMAT: {label}")
        self._log(f"⚡ Viral format applied: {label}")

    # ---- TTS engine (§7.4 / §9.3) -----------------------------------------
    def _tts_engine_code(self) -> str:
        label = self.tts_engine_label.get()
        return next((code for code, text in ENGINE_LABELS.items() if text == label), "edge_tts")

    def _set_tts_engine(self, code: str) -> None:
        if code not in ENGINE_LABELS:
            code = "edge_tts"
        self.tts_engine_label.set(ENGINE_LABELS[code])
        self._engine_changed()

    def _pitch_changed(self, _value=None) -> None:
        if hasattr(self, "edge_pitch_value"):
            self._safe_label(self.edge_pitch_value, _format_edge_pitch(self.edge_pitch_hz.get()))
        self.cfg["edge_tts_pitch"] = _format_edge_pitch(self.edge_pitch_hz.get())

    def _engine_changed(self) -> None:
        import importlib.util
        code = self._tts_engine_code()
        self.cfg["tts_engine"] = code
        save_config(self.cfg)
        has = lambda name: importlib.util.find_spec(name) is not None  # noqa: E731
        mms = has("transformers") and has("torch")
        piper = has("piper")
        key = bool(os.environ.get("ELEVENLABS_API_KEY"))
        env_path = self.gemini_env.get().strip()
        if env_path and Path(env_path).is_file():
            try:
                from dotenv import dotenv_values
                key = key or bool(dotenv_values(env_path).get("ELEVENLABS_API_KEY"))
            except Exception:
                pass
        self.engine_status.set(
            "✅ Edge TTS: Ready  |  "
            + (f"✅ MMS Myanmar: {'installed' if mms else 'not installed'}  |  " if mms else "⚪ MMS Myanmar: not installed  |  ")
            + (f"✅ Piper: {'installed' if piper else 'not installed'}  |  " if piper else "")
            + ("✅ ElevenLabs: key found" if key else "⚪ ElevenLabs: no API key")
        )

    def _preview_voice(self) -> None:
        """Generate and play a short sample of the selected voice (§9.3)."""
        voice = self.edge_voice.get().strip()
        engine_code = self._tts_engine_code()
        rate = _format_edge_rate(self.edge_rate_percent.get())
        pitch = _format_edge_pitch(self.edge_pitch_hz.get())
        env_file = self.gemini_env.get().strip()
        direction = self.gemini_direction.get().strip()
        self._log(f"▶ Generating 3 s voice preview — {voice} / {ENGINE_LABELS.get(engine_code, engine_code)}")

        def work() -> None:
            try:
                from core import EdgeTTSEngine
                from tts_engine import create_tts_engines
                ffmpeg = _tool_path(self.cfg, "ffmpeg")
                cache = LOGS_DIR / "voice_preview"
                cache.mkdir(parents=True, exist_ok=True)
                if engine_code == "gemini_tts" and voice.startswith("gemini:"):
                    from gemini_tts import GeminiTTSEngine
                    chosen = GeminiTTSEngine(ffmpeg=ffmpeg, work_dir=cache / "gemini",
                                             rate=rate, env_file=env_file, direction=direction,
                                             log=self._log)
                else:
                    if engine_code == "gemini_tts":
                        self._log("⚠️ Gemini TTS needs a gemini: voice — previewing with Edge TTS.")
                    edge = EdgeTTSEngine(ffmpeg=ffmpeg, work_dir=cache, rate=rate, pitch=pitch,
                                         log=self._log, use_ssml=bool(self.edge_tts_ssml.get()))
                    engines = create_tts_engines(ffmpeg=ffmpeg, work_dir=cache, rate=rate, pitch=pitch,
                                                 env_file=env_file, direction=direction, log=self._log,
                                                 edge_engine=edge)
                    chosen = engines.get(engine_code) or engines.get("edge_tts")
                if chosen is None:
                    raise RuntimeError("No TTS engine available on this machine")
                preview_text = ENGLISH_PREVIEW_TEXT if self._target_code() == "en" else PREVIEW_TEXT
                wav = Path(chosen.generate_channel99_voice(preview_text, voice))
                if not wav.is_file():
                    raise RuntimeError("Preview audio was not produced")
                try:
                    import winsound
                    winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
                except ImportError:
                    os.startfile(str(wav))
                self._log("✅ Voice preview playing.")
            except Exception as exc:  # noqa: BLE001
                self._log(f"⚠️ Voice preview failed: {exc}")

        threading.Thread(target=work, daemon=True).start()

    # ---- Crop (§5) ---------------------------------------------------------
    def _crop_values(self) -> dict:
        return normalize_crop({
            "enabled": bool(self.crop_enabled.get()),
            "mode": "manual",
            "x_percent": self._clampf(self.crop_x.get()),
            "y_percent": self._clampf(self.crop_y.get()),
            "width_percent": self._clampf(self.crop_w.get(), 0.02, 1.0),
            "height_percent": self._clampf(self.crop_h.get(), 0.02, 1.0),
            "aspect_lock": self.crop_aspect.get(),
        })

    def _apply_crop_values(self, values: dict) -> None:
        c = normalize_crop(values)
        self.crop_enabled.set(bool(c["enabled"]))
        self.crop_x.set(c["x_percent"])
        self.crop_y.set(c["y_percent"])
        self.crop_w.set(c["width_percent"])
        self.crop_h.set(c["height_percent"])
        self.crop_aspect.set(c["aspect_lock"])
        self._crop_slider_changed()

    def _crop_toggled(self) -> None:
        self._redraw_editor_items()
        self._log(f"✂️ Crop {'ON' if self.crop_enabled.get() else 'OFF'} — video parts re-render on next run.")

    def _crop_slider_changed(self) -> None:
        c = self._crop_values()
        # Keep the crop window inside the frame.
        x = min(c["x_percent"], max(0.0, 1.0 - c["width_percent"]))
        y = min(c["y_percent"], max(0.0, 1.0 - c["height_percent"]))
        if x != c["x_percent"]:
            self.crop_x.set(x)
        if y != c["y_percent"]:
            self.crop_y.set(y)
        fw, fh = self.source_size if self.source_size[0] else (1920, 1080)
        self._safe_label(self.crop_dim_label,
                         f"x={x:.2f} y={y:.2f}  w={c['width_percent']:.2f} h={c['height_percent']:.2f}"
                         f"  ≈ {int(c['width_percent'] * fw)}×{int(c['height_percent'] * fh)} px")
        self._redraw_editor_items()

    def _reset_crop(self) -> None:
        self._apply_crop_values({"enabled": bool(self.crop_enabled.get())})
        self._log("↩️ Crop reset to full frame")

    def _apply_crop_drag(self, x: float, y: float) -> None:
        """Crop box drag: move the box or one of its 8 handles (§5.5)."""
        kind, idx, sfx, sfy, orig = self._crop_drag
        ox, oy, pw, ph = self._preview_bounds()
        dx, dy = (x - ox) / pw - sfx, (y - oy) / ph - sfy
        X, Y, W, H = orig["x_percent"], orig["y_percent"], orig["width_percent"], orig["height_percent"]
        if kind == "move":
            X = self._clampf(X + dx, 0.0, max(0.0, 1.0 - W))
            Y = self._clampf(Y + dy, 0.0, max(0.0, 1.0 - H))
        else:
            left, top, right, bottom = X, Y, X + W, Y + H
            if idx in (0, 3, 5):
                left = min(right - 0.02, X + dx)
            if idx in (2, 4, 7):
                right = max(left + 0.02, X + W + dx)
            if idx in (0, 1, 2):
                top = min(bottom - 0.02, Y + dy)
            if idx in (5, 6, 7):
                bottom = max(top + 0.02, Y + H + dy)
            X, Y, W, H = left, top, right - left, bottom - top
            ratio = ASPECT_LOCKS.get(self.crop_aspect.get())
            if ratio:
                fw, fh = self.source_size if self.source_size[0] else (1920, 1080)
                if (W * fw) / max(1.0, H * fh) > ratio:      # too wide → narrow it
                    new_w = ratio * H * fh / fw
                    if idx in (0, 3, 5):
                        X = right - new_w
                    W = new_w
                else:                                          # too tall → shorten it
                    new_h = W * fw / (ratio * fh)
                    if idx in (0, 1, 2):
                        Y = bottom - new_h
                    H = new_h
        self.crop_x.set(self._clampf(X, 0.0, 0.98))
        self.crop_y.set(self._clampf(Y, 0.0, 0.98))
        self.crop_w.set(self._clampf(W, 0.02, 1.0))
        self.crop_h.set(self._clampf(H, 0.02, 1.0))
        self._crop_slider_changed()

    def _auto_detect_crop(self) -> None:
        src = self.source.get().strip()
        if not src or not Path(src).is_file():
            messagebox.showinfo("Auto Crop", "Load a source video first.")
            return
        ffmpeg, ffprobe = _tool_path(self.cfg, "ffmpeg"), _tool_path(self.cfg, "ffprobe")
        self._log("🔍 Auto-detecting black bars (ffmpeg cropdetect)...")

        def work() -> None:
            try:
                from crop_detect import detect_black_bars
                result = detect_black_bars(ffprobe, ffmpeg, src, duration_sample=10.0)
                self.msgq.put(("crop_detected", result))
            except Exception as exc:  # noqa: BLE001
                self.msgq.put(("crop_detected", {"error": str(exc)}))

        threading.Thread(target=work, daemon=True).start()

    def _apply_crop_detection(self, payload) -> None:
        """Handle the background cropdetect result (spec §5.4)."""
        if not isinstance(payload, dict):
            return
        if payload.get("error"):
            self._log(f"⚠️ Auto crop failed: {payload['error']}")
            messagebox.showwarning("Auto Crop", str(payload["error"]))
            return
        if not payload or not all(k in payload for k in
                                  ("x_percent", "y_percent", "width_percent", "height_percent")):
            self._log("ℹ️ Auto crop: no black bars detected — keeping the full frame.")
            messagebox.showinfo("Auto Crop", "No black bars detected. The full frame is kept.")
            return
        self.crop_enabled.set(True)
        self._apply_crop_values({"enabled": True, "mode": "auto", **payload,
                                 "aspect_lock": self.crop_aspect.get()})
        self._log(f"✂️ Auto crop applied: x={payload['x_percent']:.2f} y={payload['y_percent']:.2f} "
                  f"w={payload['width_percent']:.2f} h={payload['height_percent']:.2f}")
        self.status.set("AUTO CROP READY")

    # ---- Color correction (§6) ---------------------------------------------
    def _color_values(self) -> dict:
        return normalize_color({
            "enabled": bool(self.color_enabled.get()),
            "brightness": float(self.brightness.get()),
            "contrast": float(self.contrast.get()),
            "saturation": float(self.saturation.get()),
        })

    def _apply_color_values(self, values: dict) -> None:
        c = normalize_color(values)
        self.color_enabled.set(bool(c["enabled"]))
        self.brightness.set(c["brightness"])
        self.contrast.set(c["contrast"])
        self.saturation.set(c["saturation"])
        self._color_changed()

    def _color_changed(self) -> None:
        self._refresh_preview_later()

    def _reset_color(self) -> None:
        self._apply_color_values({"enabled": bool(self.color_enabled.get())})
        self._log("↩️ Color correction reset to defaults")

    def _refresh_preview_later(self) -> None:
        """Debounce (§6.5): one frame extraction after the slider settles."""
        job = getattr(self, "_color_preview_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._color_preview_job = self.after(350, self._refresh_preview)

    # ---- Custom image overlays (§4) ----------------------------------------
    def _add_custom_overlay(self) -> None:
        if len(self.custom_overlays) >= MAX_CUSTOM_OVERLAYS:
            messagebox.showinfo("Custom Overlays", f"Maximum {MAX_CUSTOM_OVERLAYS} overlays.")
            return
        path = filedialog.askopenfilename(title="Select overlay image",
                                          filetypes=[("Image", "*.png *.jpg *.jpeg *.webp"), ("All", "*.*")])
        if not path:
            return
        self.custom_overlays.append(normalize_overlay({
            "id": len(self.custom_overlays) + 1, "path": path,
            "x_percent": 0.85, "y_percent": 0.05, "width_percent": 0.12, "opacity": 0.90, "enabled": True,
        }, len(self.custom_overlays) + 1))
        self.selected_overlay = len(self.custom_overlays) - 1
        self._rebuild_overlay_rows()
        self._redraw_editor_items()
        self._log(f"🖼️ Overlay added: {Path(path).name}")

    def _browse_custom_overlay(self, index: int) -> None:
        if not (0 <= index < len(self.custom_overlays)):
            return
        path = filedialog.askopenfilename(title="Replace overlay image",
                                          filetypes=[("Image", "*.png *.jpg *.jpeg *.webp"), ("All", "*.*")])
        if path:
            self.custom_overlays[index]["path"] = path
            self._overlay_photo_cache.clear()
            self._rebuild_overlay_rows()
            self._redraw_editor_items()

    def _remove_custom_overlay(self, index: int) -> None:
        if 0 <= index < len(self.custom_overlays):
            self.custom_overlays.pop(index)
            self._overlay_photo_cache.clear()
            self.selected_overlay = None
            for i, ov in enumerate(self.custom_overlays, 1):
                ov["id"] = i
            self._rebuild_overlay_rows()
            self._redraw_editor_items()

    def _rebuild_overlay_rows(self) -> None:
        host = getattr(self, "overlays_host", None)
        if host is None:
            return
        for child in host.winfo_children():
            child.destroy()
        self._safe_label(self.overlay_count_label, f"{len(self.custom_overlays)} / {MAX_CUSTOM_OVERLAYS}")
        self._overlay_enabled_vars: list[tk.BooleanVar] = []
        self._overlay_slider_vars: list[tk.DoubleVar] = []
        self._overlay_time_vars = []
        self._overlay_window_labels = []
        for i, ov in enumerate(self.custom_overlays):
            frame = ttk.LabelFrame(host, text=f"Image {i+1}: {Path(ov['path']).name or '(none)'}", padding=(5, 3))
            frame.grid(row=i, column=0, sticky="ew", pady=(0, 5))
            frame.columnconfigure(1, weight=1)
            enabled_var = tk.BooleanVar(value=bool(ov["enabled"]))
            self._overlay_enabled_vars.append(enabled_var)
            ttk.Checkbutton(frame, text="Enabled", variable=enabled_var,
                            command=lambda idx=i: self._overlay_toggle(idx)).grid(row=0, column=0, sticky="w")
            ttk.Button(frame, text="Browse", style="Compact.TButton",
                       command=lambda idx=i: self._browse_custom_overlay(idx)).grid(row=0, column=1, sticky="e")
            ttk.Button(frame, text="Remove", style="Compact.TButton",
                       command=lambda idx=i: self._remove_custom_overlay(idx)).grid(row=0, column=2, padx=(4, 0))
            for r, (text, key, low, high) in enumerate((
                    ("Size", "width_percent", 0.02, 1.0), ("Opacity", "opacity", 0.10, 1.0)), start=1):
                ttk.Label(frame, text=text, width=8).grid(row=r, column=0, sticky="w")
                value = float(ov[key])
                value_var = tk.DoubleVar(value=value)
                self._overlay_slider_vars.append(value_var)
                value_label = ttk.Label(frame, text=f"{value*100:.0f}%", width=6, anchor="e")

                def on_change(v, idx=i, k=key, lbl=value_label):
                    self._overlay_slider(idx, k, v)
                    self._safe_label(lbl, f"{float(v)*100:.0f}%")

                ttk.Scale(frame, from_=low, to=high, orient="horizontal", variable=value_var,
                          command=on_change).grid(row=r, column=1, sticky="ew", pady=1)
                value_label.grid(row=r, column=2, padx=(6, 0))

            start_var = tk.StringVar(value=format_time_seconds(ov.get("start_sec", 0.0)))
            end_var = tk.StringVar(value=format_time_seconds(ov.get("end_sec")))
            self._overlay_time_vars.append((start_var, end_var))
            for row, text, var, which in ((3, "Start", start_var, "start"), (4, "End", end_var, "end")):
                ttk.Label(frame, text=text, width=8).grid(row=row, column=0, sticky="w", pady=(3, 0))
                entry = ttk.Entry(frame, textvariable=var, width=8)
                entry.grid(row=row, column=1, sticky="w", pady=(3, 0))
                entry.bind("<FocusOut>", lambda _e, idx=i, k=which, v=var: self._overlay_time_finish(idx, k, v))
                entry.bind("<Return>", lambda _e, idx=i, k=which, v=var: self._overlay_time_finish(idx, k, v))
                ttk.Button(frame, text="Use preview", style="Compact.TButton",
                           command=lambda idx=i, k=which, v=var: self._overlay_time_from_preview(idx, k, v)
                           ).grid(row=row, column=2, padx=(4, 0), pady=(3, 0))
                var.trace_add("write", lambda *_a, idx=i, k=which, v=var: self._overlay_time_changed(idx, k, v))
            window_label = ttk.Label(frame, text="", style="Muted.TLabel")
            self._overlay_window_labels.append(window_label)
            window_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(3, 0))
            self._overlay_window_hint(i)
            ttk.Label(frame, text="Position: drag on preview", style="Muted.TLabel").grid(
                row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _overlay_toggle(self, index: int) -> None:
        if 0 <= index < len(self.custom_overlays) and index < len(self._overlay_enabled_vars):
            self.custom_overlays[index]["enabled"] = bool(self._overlay_enabled_vars[index].get())
            self._redraw_editor_items()

    def _overlay_slider(self, index: int, key: str, value) -> None:
        if 0 <= index < len(self.custom_overlays):
            self.custom_overlays[index][key] = float(value)
            self._redraw_editor_items()

    def _overlay_window_hint(self, index: int) -> None:
        labels = getattr(self, "_overlay_window_labels", [])
        if not (0 <= index < len(labels)) or not (0 <= index < len(self.custom_overlays)):
            return
        ov = self.custom_overlays[index]
        start = format_time_seconds(ov.get("start_sec", 0.0)) or "0:00"
        end = format_time_seconds(ov.get("end_sec")) or "end of video"
        self._safe_label(labels[index], f"Visible {start} → {end} · blank End = whole video")

    def _overlay_time_changed(self, index: int, which: str, var: tk.StringVar) -> None:
        if not (0 <= index < len(self.custom_overlays)):
            return
        raw = str(var.get()).strip()
        parsed = parse_time_seconds(raw) if raw else (0.0 if which == "start" else None)
        if raw and parsed is None:
            self._overlay_window_hint(index)   # keep the last valid value while typing
            return
        self.custom_overlays[index]["start_sec" if which == "start" else "end_sec"] = parsed
        self._overlay_window_hint(index)
        self._redraw_editor_items()

    def _overlay_time_finish(self, index: int, which: str, var: tk.StringVar) -> None:
        raw = str(var.get()).strip()
        if not raw:
            self._overlay_time_changed(index, which, var)
            return
        parsed = parse_time_seconds(raw)
        if parsed is not None:
            var.set(format_time_seconds(parsed))

    def _overlay_time_from_preview(self, index: int, which: str, var: tk.StringVar) -> None:
        var.set(format_time_seconds(self.preview_time.get()))

    def _commit_overlay_times(self) -> None:
        """Validate and commit the Start/End text fields before a job starts."""
        for i, ov in enumerate(self.custom_overlays):
            pair = self._overlay_time_vars[i] if i < len(self._overlay_time_vars) else None
            start_raw = str(pair[0].get()).strip() if pair else format_time_seconds(ov.get("start_sec", 0.0))
            end_raw = str(pair[1].get()).strip() if pair else format_time_seconds(ov.get("end_sec"))
            start = parse_time_seconds(start_raw) if start_raw else 0.0
            if start is None:
                raise ValueError(f"Image {i+1} start time must be MM:SS (like 0:05) or seconds.")
            end = parse_time_seconds(end_raw) if end_raw else None
            if end_raw and end is None:
                raise ValueError(f"Image {i+1} end time must be MM:SS (like 0:12) or seconds.")
            if end is not None and end <= start:
                raise ValueError(f"Image {i+1} end time must be after its start time.")
            ov["start_sec"] = float(start)
            ov["end_sec"] = float(end) if end is not None else None

    def _ducking_values(self) -> dict:
        return {"enabled": bool(self.ducking_enabled.get()),
                "duck_volume_percent": int(self.duck_volume_percent.get()),
                "fade_ms": 200}

    def _apply_ducking_values(self, values: dict) -> None:
        d = dict(values or {})
        self.ducking_enabled.set(bool(d.get("enabled", False)))
        self.duck_volume_percent.set(int(d.get("duck_volume_percent", 30) or 30))
        self._safe_label(self.duck_value_label, f"{self.duck_volume_percent.get()}%")

    # ---- Batch queue (§10) --------------------------------------------------
    def _sync_batch_files(self) -> None:
        self.batch_files = [item.path for item in self.batch_manager.queue]
        self._update_start_button_label()

    def _refresh_batch_view(self) -> None:
        tree = getattr(self, "batch_tree", None)
        if tree is None:
            return
        selected = tree.selection()
        children = tree.get_children()
        if children:
            tree.delete(*children)
        for i, item in enumerate(self.batch_manager.queue):
            name, status, progress, duration, error = item.row_values()
            tree.insert("", "end", iid=str(i), values=(f"{i+1}. {name}", status, progress, duration, error))
        summary = self.batch_manager.get_summary()
        self._safe_label(self.batch_summary,
                         f"{summary['total']} queued · ✅ {summary['done']} done · ❌ {summary['failed']} failed"
                         f" · ⏭️ {summary['skipped']} skipped")
        if selected:
            for iid in selected:
                if tree.exists(iid):
                    tree.selection_add(iid)
        self._sync_batch_files()

    def _update_batch_row(self, item) -> None:
        """Repaint only the active row during progress ticks (keeps the tree smooth)."""
        tree = getattr(self, "batch_tree", None)
        if tree is None or item is None:
            return
        try:
            index = self.batch_manager.index_of(item)
        except ValueError:
            return
        iid = str(index)
        if not tree.exists(iid):
            return
        name, status, progress, duration, error = item.row_values()
        tree.item(iid, values=(f"{index + 1}. {name}", status, progress, duration, error))

    def _batch_selected_index(self) -> int:
        sel = self.batch_tree.selection()
        if not sel:
            return -1
        try:
            return int(sel[0])
        except (ValueError, TypeError):
            return -1

    def _batch_move_up(self) -> None:
        idx = self._batch_selected_index()
        if self.batch_manager.move_up(idx):
            self._refresh_batch_view()
            self.batch_tree.selection_set(str(max(0, idx - 1)))

    def _batch_move_down(self) -> None:
        idx = self._batch_selected_index()
        if self.batch_manager.move_down(idx):
            self._refresh_batch_view()
            self.batch_tree.selection_set(str(idx + 1))

    def _batch_remove(self) -> None:
        idx = self._batch_selected_index()
        if idx >= 0:
            self.batch_manager.remove_item(idx)
            self._refresh_batch_view()

    def _batch_retry_selected(self) -> None:
        idx = self._batch_selected_index()
        item = self.batch_manager.item_at(idx)
        if item and item.status in ("failed", "skipped", "done"):
            item.status = "pending"
            item.progress = 0.0
            item.error_message = ""
            item.output_path = None
            item.duration = 0.0
            self._refresh_batch_view()

    def _batch_drag_start(self, event) -> None:
        iid = self.batch_tree.identify_row(event.y)
        self._batch_drag_iid = iid or None

    def _batch_drag_release(self, event) -> None:
        source_iid, self._batch_drag_iid = self._batch_drag_iid, None
        target_iid = self.batch_tree.identify_row(event.y)
        if not source_iid or not target_iid or source_iid == target_iid:
            return
        try:
            source, target = int(source_iid), int(target_iid)
        except (TypeError, ValueError):
            return
        if self.batch_manager.move_to(source, target):
            self._refresh_batch_view()
            self.batch_tree.selection_set(str(target))

    def _batch_preview(self) -> None:
        item = self.batch_manager.item_at(self._batch_menu_index)
        if item is not None and item.path.is_file():
            self._load_source(str(item.path))
            self.job_display.set(f"Batch preview: {item.path.name}")

    def _batch_retry(self) -> None:
        count = self.batch_manager.retry_failed()
        self._refresh_batch_view()
        self._log(f"🔄 Retry: {count} item(s) put back in the queue")

    def _batch_context_menu(self, event) -> None:
        iid = self.batch_tree.identify_row(event.y)
        if not iid:
            return
        self.batch_tree.selection_set(iid)
        self._batch_menu_index = int(iid)
        try:
            self.batch_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.batch_menu.grab_release()

    def _batch_open_folder(self) -> None:
        item = self.batch_manager.item_at(self._batch_menu_index)
        if item is None:
            return
        folder = _job_dir_for(item.path)
        if folder.is_dir():
            os.startfile(str(folder))
        elif item.output_path and item.output_path.parent.is_dir():
            os.startfile(str(item.output_path.parent))
        else:
            messagebox.showinfo("Batch Queue", "No job folder yet for this video.")

    def _skip_completed_changed(self) -> None:
        self.batch_manager.skip_completed = bool(self.skip_completed.get())
        self.cfg["batch_skip_completed"] = bool(self.skip_completed.get())
        save_config(self.cfg)

    # ---- Drag and drop (§9.1) ----------------------------------------------
    def _on_file_drop(self, event) -> None:
        try:
            files = self.tk.splitlist(event.data)
        except Exception:
            return
        videos = [Path(f) for f in files if Path(str(f)).suffix.lower() in BATCH_VIDEO_EXTS]
        if not videos:
            return
        self._clear_batch()
        self._load_source(str(videos[0]))
        if len(videos) > 1:
            self.batch_manager.add_files(videos)
            self._refresh_batch_view()
            self._log(f"📂 Drag-and-drop: {len(videos)} video(s) added to batch queue")
        else:
            self._log(f"📂 Drag-and-drop: {videos[0].name} loaded")

    # ---- Progress ETA (§9.4) -----------------------------------------------
    def _update_progress_eta(self, current: int, total: int, elapsed: float) -> str:
        if current <= 0 or total <= 0 or elapsed <= 0:
            return "~calculating"
        rate = elapsed / current
        remaining = max(0.0, rate * (total - current))
        mins, secs = divmod(int(remaining), 60)
        return f"~{mins}m {secs:02d}s remaining · {rate:.1f}s/item"

    # ---- Error suggestions (§9.5) ------------------------------------------
    @staticmethod
    def suggest_for_error(message: str) -> str:
        for needle, hint_text in ERROR_SUGGESTIONS.items():
            if needle.lower() in str(message).lower():
                return hint_text
        return "Check jobs/<job>/run.log for the full child-process log."

    # ================================================================ end V2

    def _open_text_editor(self, segment: int | None = None) -> None:
        """Edit Text: read the English line next to its Burmese narration and fix the Burmese by hand."""
        source_text = self.source.get().strip()
        job = self.current_job or (_job_dir_for(Path(source_text)) if source_text else None)
        if job is None or not (job / "smart_sync_plan.json").is_file():
            messagebox.showinfo("Edit Text", "ဒီ video အတွက် translation plan မရှိသေးပါ။ START နှိပ်ပြီး translation ပြီးမှ ပြင်လို့ရပါမယ်။")
            return
        existing = getattr(self, "_text_editor", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            if segment:
                existing._show(max(1, min(int(segment), len(existing.segments))))
            return
        self._text_editor = NarrationEditor(
            self, job, start_segment=segment or 1, on_saved=lambda n, _t: self._narration_saved(n),
            colors={"bg": BG, "panel": PANEL, "fg": FG, "muted": MUTED},
        )

    def _narration_saved(self, segment: int) -> None:
        self._log(f"✏️ Line {segment} saved — START / RESUME gives a new voice only to the edited line(s).")

    def _open_output(self) -> None:
        if not self.source.get().strip():
            messagebox.showinfo("Output", "Source video မရွေးရသေးပါ။")
            return
        job = _job_dir_for(Path(self.source.get().strip()))
        if not job.exists():
            messagebox.showinfo("Output", "Job folder မရှိသေးပါ။")
            return
        os.startfile(str(job))


if __name__ == "__main__":
    App().mainloop()
