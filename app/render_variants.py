"""Render-only second passes: saved ratio profiles and job variant metadata.

A first START / RESUME run pays for Whisper + Gemini and for the narration
voice.  ``Render This Ratio`` (and two-ratio START) reuses those artifacts: it reads the job's
plan/audio, swaps only the output canvas and text settings, and writes a new
video file without calling the API or Edge TTS again.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any

STATE_FILE = "render_variants.json"
KNOWN_RATIOS = ("16:9", "9:16", "1:1", "Original")

# Values that can reasonably differ between a 16:9 upload and a 9:16 upload.
# They are kept per ratio in config.json -> ratio_profiles.
PROFILE_KEYS = (
    "output_ratio", "frame_fill", "video_quality", "zoom_factor", "mirror_video",
    "title_text", "title_size", "title_position",
    "logo_enabled", "logo_path", "logo_width_frac", "logo_opacity", "logo_position",
    "extra_text", "extra_text_size", "extra_text_opacity", "extra_text_position",
    "subtitles", "crop", "color_correction", "custom_overlays", "audio_ducking",
)

# Worker settings use the older "font_size" names inside their overlays dict.
OVERLAY_PROFILE_MAP = {
    "title_font_size": "title_size",
    "extra_text_font_size": "extra_text_size",
}

VOICE_CONFIG_KEYS = (
    "voice", "edge_tts_voice", "edge_tts_rate", "edge_tts_pitch", "tts_engine",
    "edge_tts_ssml", "target_language", "gemini_direction", "gemini_env_path",
    "short_pauses", "voice_volume_percent",
)


def ratio_key(ratio: object) -> str:
    """Stable, filename-safe key: ``9:16`` -> ``9x16``, ``Original`` -> ``Original``."""
    text = str(ratio or "16:9").strip().replace("/", ":")
    key = re.sub(r"[^0-9A-Za-z]+", "x", text).strip("x")
    return key or "16x9"


def canonical_ratio(value: object, fallback: str = "16:9") -> str:
    """Return a UI ratio label, accepting saved keys such as ``9x16`` too."""
    text = str(value or "").strip()
    for candidate in KNOWN_RATIOS:
        if ratio_key(candidate) == ratio_key(text or fallback):
            return candidate
    return fallback


# A vertical / square upload needs different text sizes and positions from a
# 16:9 upload. These are only applied when the user has not saved a style yet.
RATIO_STYLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "9:16": {
        "frame_fill": "crop",
        "title_size": 64,
        "title_position": [0.5, 0.08],
        "extra_text_size": 44,
        "extra_text_position": [0.5, 0.88],
        "subtitles": {"size": 48, "position": [0.5, 0.80]},
    },
    "1:1": {
        "frame_fill": "crop",
        "title_size": 58,
        "title_position": [0.5, 0.10],
        "extra_text_size": 40,
        "extra_text_position": [0.5, 0.86],
        "subtitles": {"size": 44, "position": [0.5, 0.82]},
    },
}


def default_profile_for_ratio(ratio: object) -> dict[str, Any]:
    """Recommended starting style for a ratio that has no saved profile yet."""
    wanted = canonical_ratio(ratio)
    profile = copy.deepcopy(RATIO_STYLE_DEFAULTS.get(wanted, {}))
    profile["output_ratio"] = wanted
    return profile


def variant_filename(source_stem: str, ratio: object, suffix: str = "English") -> str:
    """Name for a ratio-specific copy, e.g. ``video_English_9x16.mp4``."""
    stem = re.sub(r"[^\w\-]+", "_", str(source_stem or "video"), flags=re.UNICODE).strip("_")
    safe_suffix = re.sub(r"[^0-9A-Za-z]+", "_", str(suffix or "English")).strip("_") or "English"
    return f"{stem or 'video'}_{safe_suffix}_{ratio_key(ratio)}.mp4"


def final_output_filename(source_stem: str, ratio: object, suffix: str,
                          *, two_ratios: bool = False, render_only: bool = False) -> str:
    """User-facing filename. Two-ratio jobs name both files by ratio so there
    is exactly one output per ratio and no leftover unsuffixed duplicate."""
    if two_ratios or render_only:
        return variant_filename(source_stem, ratio, suffix)
    stem = re.sub(r"[^\w\-]+", "_", str(source_stem or "video"), flags=re.UNICODE).strip("_") or "video"
    safe_suffix = re.sub(r"[^0-9A-Za-z]+", "_", str(suffix or "English")).strip("_") or "English"
    return f"{stem}_{safe_suffix}.mp4"


def profile_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Extract the per-ratio visual settings from a controller/worker settings dict."""
    source = dict(settings or {})
    overlays = source.get("overlays") or {}
    if not isinstance(overlays, dict):
        overlays = {}
    profile: dict[str, Any] = {}
    for key in PROFILE_KEYS:
        if key in source:
            profile[key] = copy.deepcopy(source[key])
    for worker_key, profile_key in OVERLAY_PROFILE_MAP.items():
        if worker_key in overlays:
            profile[profile_key] = copy.deepcopy(overlays[worker_key])
    for key in PROFILE_KEYS:
        if key in overlays and key not in profile:
            profile[key] = copy.deepcopy(overlays[key])
    profile["output_ratio"] = str(source.get("output_ratio") or profile.get("output_ratio") or "16:9")
    return profile


def apply_profile_to_settings(settings: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``settings`` with a saved ratio profile merged in."""
    merged = copy.deepcopy(settings or {})
    overlays = merged.setdefault("overlays", {})
    if not isinstance(overlays, dict):
        overlays = {}
        merged["overlays"] = overlays
    overlay_members = {
        "title_text", "title_position", "logo_enabled", "logo_path", "logo_width_frac",
        "logo_opacity", "logo_position", "extra_text", "extra_text_position",
        "extra_text_opacity", "subtitles", "custom_overlays",
    }
    for key in PROFILE_KEYS:
        if key not in (profile or {}):
            continue
        value = copy.deepcopy(profile[key])
        # Merge nested style dicts (subtitles, crop, ducking...) so a ratio
        # profile can change size/position without resetting font or colour.
        existing = overlays.get(key) if key in overlays else merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            combined = dict(existing or {})
            combined.update(value)
            value = combined
        if key in ("title_size", "extra_text_size"):
            worker_key = "title_font_size" if key == "title_size" else "extra_text_font_size"
            overlays[worker_key] = value
            overlays[key] = value
            merged[key] = value
        elif key in overlay_members:
            overlays[key] = value
            merged[key] = value
        elif key in overlays:
            overlays[key] = value
        else:
            merged[key] = value
    if "output_ratio" in (profile or {}):
        merged["output_ratio"] = str(profile["output_ratio"])
    return merged


def state_path(job_dir: Path) -> Path:
    return Path(job_dir) / STATE_FILE


def load_state(job_dir: Path) -> dict[str, Any]:
    path = state_path(job_dir)
    data: Any = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    variants = data.get("variants")
    data["version"] = 1
    data["variants"] = variants if isinstance(variants, dict) else {}
    return data


def save_state(job_dir: Path, state: dict[str, Any]) -> Path:
    path = state_path(Path(job_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def missing_artifacts(job_dir: Path) -> list[str]:
    """Files that must already exist before a render-only pass can be trusted."""
    job = Path(job_dir)
    edge = job / "edge_tts_smart_sync"
    required = (
        ("script / timeline", job / "smart_sync_plan.json"),
        ("voice timeline", edge / "smart_sync_plan_edge_tts.json"),
        ("generated narration audio", edge / "voice_edge_tts.wav"),
    )
    missing: list[str] = []
    for label, path in required:
        if not path.is_file() or path.stat().st_size < 1000:
            missing.append(label)
    return missing


def voice_config(job_dir: Path) -> dict[str, Any]:
    """Voice/rate/engine used by the first pass, so no setting can force new TTS."""
    state = load_state(job_dir)
    saved = state.get("voice")
    if not isinstance(saved, dict):
        edge_plan = Path(job_dir) / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json"
        if edge_plan.is_file():
            try:
                data = json.loads(edge_plan.read_text(encoding="utf-8"))
                saved = {"voice": data.get("tts_voice", "")}
            except Exception:
                saved = {}
        else:
            saved = {}
    return saved if isinstance(saved, dict) else {}


def restore_voice_config(job_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Return render settings with the original voice configuration restored."""
    merged = copy.deepcopy(settings or {})
    saved = voice_config(job_dir)
    for key in VOICE_CONFIG_KEYS:
        if key in saved:
            merged[key] = copy.deepcopy(saved[key])
    return merged


def prepare_two_ratio_settings(settings: dict[str, Any], first_profile: dict[str, Any],
                               second_profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """Build both settings for the dedicated Two-Ratio tab.

    Each panel owns a complete style profile, so neither video can inherit the
    other's text sizes / positions.  This helper only merges visual settings;
    voice, rate, language and API configuration stay shared and are copied from
    ``settings`` unchanged.
    """
    base = dict(settings or {})
    first_profile = dict(first_profile or {})
    second_profile = dict(second_profile or {})
    first_ratio = canonical_ratio(first_profile.get("output_ratio") or base.get("output_ratio"), "16:9")
    fallback = "16:9" if first_ratio != "16:9" else "9:16"
    second_ratio = canonical_ratio(second_profile.get("output_ratio") or base.get("second_output_ratio"), fallback)
    if second_ratio == first_ratio:
        raise ValueError("Two-video mode needs two different ratios. Choose a different Ratio for Video B.")

    first_profile["output_ratio"] = first_ratio
    second_profile["output_ratio"] = second_ratio
    first = apply_profile_to_settings(base, first_profile)
    second = apply_profile_to_settings(base, second_profile)
    prepared: list[dict[str, Any]] = []
    for item, ratio, other in ((first, first_ratio, second_ratio), (second, second_ratio, first_ratio)):
        item["output_ratio"] = ratio
        item["second_output_ratio"] = other
        item["export_two_ratios"] = True
        zoom = float(item.get("zoom_factor", 1.0))
        if not 1.0 <= zoom <= 1.25:
            raise ValueError(f"Zoom must be between 1.00 and 1.25 (Ratio {ratio}).")
        prepared.append(item)
    return prepared[0], prepared[1], first_ratio, second_ratio


def prepare_second_ratio(settings: dict[str, Any], job_dir: Path,
                         profiles: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Build the render-only settings for the second ratio of a two-ratio job."""
    base = dict(settings or {})
    primary = canonical_ratio(base.get("output_ratio"), "16:9")
    fallback = "16:9" if primary != "16:9" else "9:16"
    second = canonical_ratio(base.get("second_output_ratio"), fallback)
    if second == primary:
        second = next((item for item in KNOWN_RATIOS if item != primary), fallback)

    merged = dict(base)
    merged["output_ratio"] = second
    saved_profiles = profiles if isinstance(profiles, dict) else {}
    profile = saved_profiles.get(ratio_key(second)) or saved_profiles.get(second)
    if not isinstance(profile, dict) or not profile:
        profile = default_profile_for_ratio(second)
    merged = apply_profile_to_settings(merged, profile)
    merged["output_ratio"] = second          # never let a legacy profile key win
    return restore_voice_config(job_dir, merged), second


def record_variant(job_dir: Path, settings: dict[str, Any], final_path: Path,
                   source_stem: str = "", suffix: str = "") -> Path:
    """Copy a finished render to a ratio-named file and remember it in the job."""
    job = Path(job_dir)
    state = load_state(job)
    ratio = str((settings or {}).get("output_ratio") or "16:9")
    key = ratio_key(ratio)
    label = suffix or ("English" if str((settings or {}).get("target_language") or "mm").lower() == "en" else "Myanmar")
    stem = source_stem or str(Path(str((settings or {}).get("source") or "video")).stem or "video")
    final = Path(final_path)
    variant = final.parent / variant_filename(stem, ratio, label)
    if final.is_file() and final.resolve() != variant.resolve():
        shutil.copy2(final, variant)

    saved_voice: dict[str, Any] = {}
    for item in VOICE_CONFIG_KEYS:
        if item in (settings or {}):
            saved_voice[item] = copy.deepcopy(settings[item])
    if saved_voice:
        state["voice"] = saved_voice
    variants = state.setdefault("variants", {})
    if not isinstance(variants, dict):
        variants = {}
        state["variants"] = variants
    variants[key] = {
        "ratio": ratio,
        "output": str(variant),
        "final": str(final),
        "suffix": label,
        "profile": profile_from_settings(settings),
    }
    save_state(job, state)
    return variant
