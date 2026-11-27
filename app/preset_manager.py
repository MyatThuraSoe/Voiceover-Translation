"""Templates & Presets (V2, 2026-11): save / load / delete / default named settings presets.

User presets live in presets.json (app root); the three factory presets ship in
factory_presets.json and can never be deleted (spec §3). A preset captures every
user-facing production setting so one click restores a complete configuration.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from filter_chain import MAX_CUSTOM_OVERLAYS, normalize_overlay

PRESET_VERSION = 1
MAX_NAME_LENGTH = 50

# The complete list of preset keys (spec §3.4). Everything capture/apply touches
# must be listed here so a save→load round-trip is lossless.
PRESET_KEYS = (
    "output_ratio", "frame_fill", "video_quality", "zoom_factor", "mirror_video",
    "voice_volume_percent", "short_pauses", "edge_voice", "edge_rate_percent",
    "edge_pitch_hz", "gemini_direction", "tts_engine", "edge_tts_ssml", "target_language",
    "title_text", "title_size", "title_position", "title_box_enabled", "title_box_opacity",
    "logo_enabled", "logo_path", "logo_width_frac", "logo_opacity", "logo_position",
    "extra_text", "extra_text_size", "extra_text_opacity", "extra_text_position",
    "subtitles", "crop", "blur_boxes", "custom_overlays", "color_correction", "audio_ducking",
)

FACTORY_PRESETS: dict[str, dict[str, Any]] = {
    "YouTube Long Form": {
        "is_factory": True,
        "settings": {
            "output_ratio": "16:9", "frame_fill": "blur", "video_quality": "1080",
            "zoom_factor": 1.0, "mirror_video": False,
            "voice_volume_percent": 100, "short_pauses": True,
            "edge_voice": "my-MM-ThihaNeural", "edge_rate_percent": 0, "edge_pitch_hz": 0,
            "tts_engine": "edge_tts",
            "edge_tts_ssml": False,
            "gemini_direction": "",
            "title_text": "", "title_size": 48, "title_position": [0.5, 0.12],
            "title_box_enabled": True, "title_box_opacity": 57,
            "logo_enabled": False, "logo_path": "", "logo_width_frac": 0.12,
            "logo_opacity": 0.8, "logo_position": [0.80, 0.06],
            "extra_text": "", "extra_text_size": 32, "extra_text_opacity": 1.0,
            "extra_text_position": [0.50, 0.84],
            "subtitles": {}, "crop": {"enabled": False}, "custom_overlays": [],
            "color_correction": {"enabled": False, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0},
            "audio_ducking": {"enabled": False, "duck_volume_percent": 30, "fade_ms": 200},
        },
    },
    "TikTok / Reels Viral": {
        "is_factory": True,
        "settings": {
            "output_ratio": "9:16", "frame_fill": "blur", "video_quality": "1080",
            "zoom_factor": 1.25, "mirror_video": True,
            "voice_volume_percent": 100, "short_pauses": True,
            "edge_voice": "my-MM-ThihaNeural", "edge_rate_percent": 0, "edge_pitch_hz": 0,
            "tts_engine": "edge_tts",
            "edge_tts_ssml": False,
            "gemini_direction": "",
            "title_text": "", "title_size": 48, "title_position": [0.5, 0.08],
            "title_box_enabled": True, "title_box_opacity": 57,
            "logo_enabled": False, "logo_path": "", "logo_width_frac": 0.12,
            "logo_opacity": 0.8, "logo_position": [0.80, 0.03],
            "extra_text": "", "extra_text_size": 32, "extra_text_opacity": 1.0,
            "extra_text_position": [0.50, 0.88],
            "subtitles": {}, "crop": {"enabled": False}, "custom_overlays": [],
            "color_correction": {"enabled": False, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0},
            "audio_ducking": {"enabled": False, "duck_volume_percent": 30, "fade_ms": 200},
        },
    },
    "Facebook Square": {
        "is_factory": True,
        "settings": {
            "output_ratio": "1:1", "frame_fill": "blur", "video_quality": "720",
            "zoom_factor": 1.1, "mirror_video": False,
            "voice_volume_percent": 100, "short_pauses": True,
            "edge_voice": "my-MM-NilarNeural", "edge_rate_percent": 0, "edge_pitch_hz": 0,
            "tts_engine": "edge_tts",
            "edge_tts_ssml": False,
            "gemini_direction": "",
            "title_text": "", "title_size": 44, "title_position": [0.5, 0.12],
            "title_box_enabled": True, "title_box_opacity": 57,
            "logo_enabled": False, "logo_path": "", "logo_width_frac": 0.12,
            "logo_opacity": 0.8, "logo_position": [0.80, 0.06],
            "extra_text": "", "extra_text_size": 30, "extra_text_opacity": 1.0,
            "extra_text_position": [0.50, 0.84],
            "subtitles": {}, "crop": {"enabled": False}, "custom_overlays": [],
            "color_correction": {"enabled": False, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0},
            "audio_ducking": {"enabled": False, "duck_volume_percent": 30, "fade_ms": 200},
        },
    },
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_name(name: str) -> str:
    """Trim, collapse whitespace, cap length (spec §3.7)."""
    text = " ".join(str(name or "").split()).strip()
    return text[:MAX_NAME_LENGTH]


class PresetManager:
    """preset storage with factory merge, default preset, and controller capture/apply."""

    def __init__(self, app_root: Path, log=print, factory_path: Path | None = None) -> None:
        self.file_path = Path(app_root) / "presets.json"
        self.factory_path = (Path(factory_path) if factory_path is not None
                             else Path(app_root) / "factory_presets.json")
        self.presets: dict[str, dict[str, Any]] = {}
        self.default_preset: str = ""
        self.log = log
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        with self._lock:
            factory = dict(FACTORY_PRESETS)
            if self.factory_path.is_file():
                try:
                    data = json.loads(self.factory_path.read_text(encoding="utf-8"))
                    loaded = data.get("presets") if isinstance(data, dict) else None
                    if isinstance(loaded, dict):
                        factory = {k: v for k, v in loaded.items() if isinstance(v, dict)}
                except Exception as exc:
                    self.log(f"⚠️ factory_presets.json unreadable ({exc}) — built-in factory presets used")
            self.presets = {}
            for name, entry in factory.items():
                if isinstance(entry, dict):
                    self.presets[str(name)] = {
                        "created_at": entry.get("created_at", _now_iso()),
                        "is_factory": True,
                        "settings": dict(entry.get("settings") or {}),
                    }
            user_presets: dict[str, Any] = {}
            default = ""
            if self.file_path.is_file():
                try:
                    data = json.loads(self.file_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and int(data.get("version", PRESET_VERSION)) <= PRESET_VERSION:
                        user_presets = data.get("presets") or {}
                        default = str(data.get("default_preset") or "")
                except Exception as exc:
                    # Corrupted file: fall back to factory presets and log (spec §3.7)
                    self.log(f"⚠️ presets.json is corrupted ({exc}) — factory presets only; re-save to recover")
            for name, entry in user_presets.items():
                if isinstance(entry, dict) and str(name).strip():
                    self.presets[str(name)] = {
                        "created_at": entry.get("created_at", _now_iso()),
                        "is_factory": False,
                        "settings": dict(entry.get("settings") or {}),
                    }
            self.default_preset = default if default in self.presets else ""

    def _save(self) -> None:
        with self._lock:
            user = {
                "version": PRESET_VERSION,
                "default_preset": self.default_preset,
                "presets": {k: v for k, v in self.presets.items() if not v.get("is_factory")},
            }
            tmp = self.file_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.file_path)

    # ------------------------------------------------------------ queries
    def get_preset_names(self) -> list[str]:
        with self._lock:
            factory_first = [k for k, v in self.presets.items() if v.get("is_factory")]
            user = [k for k, v in self.presets.items() if not v.get("is_factory")]
            return factory_first + sorted(user, key=str.casefold)

    def is_factory(self, name: str) -> bool:
        with self._lock:
            return bool((self.presets.get(name) or {}).get("is_factory"))

    def get_settings(self, name: str) -> dict:
        with self._lock:
            entry = self.presets.get(name)
        return dict(entry["settings"]) if entry else {}

    def get_default_name(self) -> str:
        with self._lock:
            return self.default_preset

    # ------------------------------------------------------------ mutations
    def find_preset(self, name: str) -> str | None:
        """Case-insensitive lookup (spec §3.7)."""
        wanted = sanitize_name(name).casefold()
        with self._lock:
            for key in self.presets:
                if key.casefold() == wanted:
                    return key
        return None

    def save_preset(self, name: str, settings: dict) -> str:
        """Save (or overwrite) a named preset. Returns the canonical stored name."""
        clean = sanitize_name(name)
        if not clean:
            raise ValueError("Preset name is empty.")
        existing = self.find_preset(clean)
        if existing and self.is_factory(existing):
            raise ValueError(f"'{existing}' is a built-in preset and cannot be overwritten.")
        key = existing or clean
        stored = {k: settings.get(k) for k in PRESET_KEYS if settings.get(k) is not None}
        with self._lock:
            self.presets[key] = {"created_at": _now_iso(), "is_factory": False, "settings": stored}
            self._save()
        return key

    def delete_preset(self, name: str) -> bool:
        with self._lock:
            entry = self.presets.get(name)
            if not entry or entry.get("is_factory"):
                return False
            del self.presets[name]
            if self.default_preset == name:
                self.default_preset = ""
            self._save()
        return True

    def set_default(self, name: str) -> None:
        with self._lock:
            if name in self.presets:
                self.default_preset = name
                self._save()

    # ------------------------------------------------------------ controller bridge
    def capture_from_controller(self, controller) -> dict:
        """Read every user-facing Tk variable into a settings dict (spec §3.6)."""
        def var(attr, fallback):
            try:
                return getattr(controller, attr).get()
            except Exception:
                return fallback

        overlays = {
            "title_text": str(controller.title_text.get() or ""),
            "title_size": int(var("title_size", 54)),
            "title_position": list(getattr(controller, "title_pos", [0.5, 0.12])),
            "title_box_enabled": bool(var("title_box_enabled", True)),
            "title_box_opacity": max(0, min(100, int(var("title_box_opacity", 57)))),
            "logo_enabled": bool(var("logo_enabled", False)),
            "logo_path": str(var("logo_path", "")),
            "logo_width_frac": float(var("logo_width_frac", 0.16)),
            "logo_opacity": float(var("logo_opacity", 0.65)),
            "logo_position": list(getattr(controller, "logo_pos", [0.80, 0.06])),
            "extra_text": str(controller.extra_text.get() or ""),
            "extra_text_size": int(var("extra_text_size", 42)),
            "extra_text_opacity": float(var("extra_text_opacity", 0.65)),
            "extra_text_position": list(getattr(controller, "extra_text_pos", [0.50, 0.84])),
        }
        try:
            overlays["subtitles"] = controller.subtitle_controls.values()
        except Exception:
            overlays["subtitles"] = {}
        return {
            "target_language": controller._target_code(),
            "output_ratio": controller.output_ratio.get(),
            "frame_fill": controller._fill_code(),
            "video_quality": controller._quality_code(),
            "zoom_factor": float(var("zoom_factor", 1.05)),
            "mirror_video": bool(var("mirror_video", True)),
            "voice_volume_percent": int(var("voice_volume", 100)),
            "short_pauses": bool(var("short_pauses", True)),
            "edge_voice": controller.edge_voice.get(),
            "edge_rate_percent": int(round(float(var("edge_rate_percent", 0)))),
            "edge_pitch_hz": int(var("edge_pitch_hz", 0)),
            "gemini_direction": str(controller.gemini_direction.get() or ""),
            "tts_engine": str(controller._tts_engine_code()),
            "edge_tts_ssml": bool(var("edge_tts_ssml", False)),
            **overlays,
            "crop": controller._crop_values(),
            "blur_boxes": [dict(b) for b in getattr(controller, "blur_boxes", [])],
            "color_correction": controller._color_values(),
            "audio_ducking": controller._ducking_values(),
            "custom_overlays": [dict(o) for o in getattr(controller, "custom_overlays", [])],
        }

    def apply_to_controller(self, controller) -> list[str]:
        """Restore a captured settings dict onto the Tk variables.

        Callers should first select the preset via get_settings(name). This
        method returns a list of warnings (missing logo file, unknown keys).
        """
        warnings: list[str] = []
        settings = getattr(controller, "_pending_preset", None) or {}

        def set_var(attr, value):
            try:
                getattr(controller, attr).set(value)
            except Exception as exc:
                warnings.append(f"{attr}: {exc}")

        if "target_language" in settings and hasattr(controller, "_set_target"):
            try:
                controller._set_target(str(settings["target_language"]))
            except Exception as exc:
                warnings.append(f"target_language: {exc}")
        if "output_ratio" in settings:
            set_var("output_ratio", settings["output_ratio"])
        if "frame_fill" in settings and hasattr(controller, "_fill_label"):
            set_var("frame_fill", controller._fill_label(settings["frame_fill"]))
        if "video_quality" in settings and hasattr(controller, "_quality_label"):
            set_var("quality", controller._quality_label(settings["video_quality"]))
        for attr in ("zoom_factor", "voice_volume", "edge_rate_percent", "edge_pitch_hz",
                     "edge_voice", "gemini_direction", "title_text", "title_size",
                     "title_box_opacity", "extra_text", "extra_text_size", "extra_text_opacity"):
            if attr in settings:
                value = settings[attr]
                if attr == "edge_voice" and hasattr(controller, "_narrator_voices"):
                    try:                     # factory/old presets may carry the other language's voice
                        voices = controller._narrator_voices()
                        if value not in voices:
                            value = voices[0]
                    except Exception:
                        pass
                if attr == "title_box_opacity":
                    try:
                        value = max(0, min(100, int(value)))
                    except (TypeError, ValueError):
                        value = 57
                set_var(attr, value)
        for attr in ("mirror_video", "short_pauses", "logo_enabled", "title_box_enabled"):
            if attr in settings:
                set_var(attr, bool(settings[attr]))
        if "logo_path" in settings:
            set_var("logo_path", str(settings["logo_path"] or ""))
            if settings["logo_path"] and not Path(str(settings["logo_path"])).is_file():
                warnings.append(f"Logo file no longer exists: {settings['logo_path']}")
        if "logo_width_frac" in settings:
            set_var("logo_width_frac", float(settings["logo_width_frac"]))
        if "logo_opacity" in settings:
            set_var("logo_opacity", float(settings["logo_opacity"]))
        if hasattr(controller, "title_pos") and "title_position" in settings:
            controller.title_pos = list(settings["title_position"])
        if hasattr(controller, "logo_pos") and "logo_position" in settings:
            controller.logo_pos = list(settings["logo_position"])
        if hasattr(controller, "extra_text_pos") and "extra_text_position" in settings:
            controller.extra_text_pos = list(settings["extra_text_position"])
        if hasattr(controller, "_apply_crop_values") and "crop" in settings:
            controller._apply_crop_values(dict(settings["crop"]))
        if "blur_boxes" in settings and hasattr(controller, "blur_boxes"):
            controller.blur_boxes = [dict(b) for b in (settings["blur_boxes"] or []) if isinstance(b, dict)]
            controller.selected_blur = None
        if hasattr(controller, "_apply_color_values") and "color_correction" in settings:
            controller._apply_color_values(dict(settings["color_correction"]))
        if hasattr(controller, "_apply_ducking_values") and "audio_ducking" in settings:
            controller._apply_ducking_values(dict(settings["audio_ducking"]))
        if "custom_overlays" in settings and hasattr(controller, "custom_overlays"):
            # Old presets have no timing fields: normalize them to start=0 /
            # end=whole video so scrubbing and rendering agree.
            controller.custom_overlays = [
                normalize_overlay(o, i + 1) for i, o in
                enumerate((settings["custom_overlays"] or [])[:MAX_CUSTOM_OVERLAYS])
            ]
            for o in controller.custom_overlays:
                if o.get("path") and not Path(str(o["path"])).is_file():
                    warnings.append(f"Overlay image no longer exists: {o['path']}")
        if "tts_engine" in settings and hasattr(controller, "_set_tts_engine"):
            controller._set_tts_engine(str(settings["tts_engine"]))
        if "edge_tts_ssml" in settings and hasattr(controller, "edge_tts_ssml"):
            set_var("edge_tts_ssml", bool(settings["edge_tts_ssml"]))
        if "subtitles" in settings and hasattr(controller, "subtitle_controls"):
            try:
                controller.subtitle_controls.load_values(dict(settings["subtitles"] or {}))
            except Exception as exc:
                warnings.append(f"subtitles: {exc}")
        return warnings
