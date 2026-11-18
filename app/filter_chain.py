"""FFmpeg filter chain builder (V2, 2026-11): crop → color → smart sync → zoom/mirror →
scale → ratio/pad → overlays → text → subtitles, in one deterministic order.

The chain is resolution-independent on input: crop / overlay values are stored as
percentages and converted to pixels against the *current* video dimensions, so a
preset made for one video lands the same on any other resolution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Exact order every build must follow (spec §11.1). Only non-empty steps appear in
# the generated graph, but their relative order never changes.
CHAIN_STEPS = ("crop", "color", "zoom_mirror", "overlays", "text", "subtitles")

ASPECT_LOCKS: dict[str, float | None] = {
    "free": None,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1.0,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
}

MAX_CUSTOM_OVERLAYS = 5


def _even(value: float) -> int:
    """Round down to a positive even number (many codecs need even w/h/x/y)."""
    number = int(round(float(value)))
    if number < 2:
        return 2
    return number - (number % 2)


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


_TIME_TOKEN_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse_time_seconds(value: Any, default: float | None = None) -> float | None:
    """Parse a time input as seconds.

    Accepts ``MM:SS``, ``H:MM:SS`` or plain seconds (int/float/string). Blank or
    invalid values return ``default`` (``None`` means whole-video/unset).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if number >= 0.0 else default
    text = str(value).strip()
    if text == "":
        return default
    match = _TIME_TOKEN_RE.match(text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        return max(0.0, (hours * 3600.0) + (minutes * 60.0) + seconds)
    try:
        number = float(text)
    except (TypeError, ValueError):
        return default
    return number if number >= 0.0 else default


def format_time_seconds(value: Any) -> str:
    """Format seconds as ``MM:SS`` or ``H:MM:SS``. Blank/unset -> empty string."""
    seconds = parse_time_seconds(value, default=None)
    if seconds is None:
        return ""
    whole = max(0, int(round(seconds * 1000.0)))
    ms = whole % 1000
    total = whole // 1000
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    base = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
    return f"{base}.{ms:03d}".rstrip("0").rstrip(".") if ms else base


def normalize_crop(crop: dict[str, Any] | None) -> dict[str, Any]:
    """Validated crop dict with sane defaults (percentages, 0..1)."""
    c = dict(crop or {})
    if not c.get("enabled", False):
        return {"enabled": False, "mode": "manual", "x_percent": 0.0, "y_percent": 0.0,
                "width_percent": 1.0, "height_percent": 1.0, "aspect_lock": "free"}
    lock = str(c.get("aspect_lock") or "free")
    return {
        "enabled": True,
        "mode": str(c.get("mode") or "manual"),
        "x_percent": _clamp01(c.get("x_percent", 0.0)),
        "y_percent": _clamp01(c.get("y_percent", 0.0)),
        "width_percent": max(0.02, min(1.0, _clamp01(c.get("width_percent", 1.0), 1.0))),
        "height_percent": max(0.02, min(1.0, _clamp01(c.get("height_percent", 1.0), 1.0))),
        "aspect_lock": lock if lock in ASPECT_LOCKS else "free",
    }


def crop_size(crop: dict[str, Any] | None, video_w: int, video_h: int) -> tuple[int, int]:
    """The effective frame size after crop (even numbers), full frame when disabled."""
    c = normalize_crop(crop)
    if not c["enabled"]:
        return int(video_w), int(video_h)
    w = _even(c["width_percent"] * video_w)
    h = _even(c["height_percent"] * video_h)
    # Never bigger than the source and never degenerate.
    w = min(w, _even(video_w))
    h = min(h, _even(video_h))
    return w, h


def build_crop_filter(crop: dict[str, Any] | None, video_w: int, video_h: int) -> str | None:
    """FFmpeg crop filter string, or None when crop is off / covers the whole frame.

    x/y are offsets of the crop window; even values keep yuv420p happy.
    """
    c = normalize_crop(crop)
    if not c["enabled"]:
        return None
    x = _even(c["x_percent"] * video_w)
    y = _even(c["y_percent"] * video_h)
    w = _even(c["width_percent"] * video_w)
    h = _even(c["height_percent"] * video_h)
    w, h = min(w, _even(video_w)), min(h, _even(video_h))
    x, y = min(x, _even(video_w) - w), min(y, _even(video_h) - h)
    if w >= video_w and h >= video_h:
        return None  # no actual cropping needed
    if w < 2 or h < 2:
        return None  # degenerate window: treat as disabled (spec §5.7)
    return f"crop={w}:{h}:{x}:{y}"


def normalize_color(cc: dict[str, Any] | None) -> dict[str, float | bool]:
    c = dict(cc or {})
    return {
        "enabled": bool(c.get("enabled", False)),
        "brightness": max(-0.5, min(0.5, float(c.get("brightness", 0.0) or 0.0))),
        "contrast": max(0.5, min(2.0, float(c.get("contrast", 1.0) or 1.0))),
        "saturation": max(0.0, min(3.0, float(c.get("saturation", 1.0) or 1.0))),
    }


def build_color_filter(cc: dict[str, Any] | None) -> str | None:
    """FFmpeg eq filter string, or None when disabled / all defaults (spec §6.4)."""
    c = normalize_color(cc)
    if not c["enabled"]:
        return None
    b, k, s = c["brightness"], c["contrast"], c["saturation"]
    if b == 0.0 and k == 1.0 and s == 1.0:
        return None
    return f"eq=brightness={b:.3f}:contrast={k:.3f}:saturation={s:.3f}"


def normalize_overlay(ov: dict[str, Any] | None, overlay_id: int = 0) -> dict[str, Any]:
    """Validated overlay dict. Missing timing means start=0 / end=whole video."""
    o = dict(ov or {})
    start = parse_time_seconds(o.get("start_sec"))
    end = parse_time_seconds(o.get("end_sec"))
    return {
        "id": int(o.get("id", overlay_id) or overlay_id),
        "path": str(o.get("path") or "").strip(),
        "x_percent": _clamp01(o.get("x_percent", 0.85), 0.85),
        "y_percent": _clamp01(o.get("y_percent", 0.05), 0.05),
        "width_percent": max(0.01, min(1.0, _clamp01(o.get("width_percent", 0.12), 0.12))),
        "opacity": max(0.05, min(1.0, float(o.get("opacity", 0.9) if o.get("opacity") is not None else 0.9))),
        "enabled": bool(o.get("enabled", True)),
        "start_sec": max(0.0, start) if start is not None else 0.0,
        "end_sec": max(0.0, end) if end is not None else None,
    }


def overlay_time_window(ov: dict[str, Any] | None) -> tuple[float, float | None]:
    """(start, end) in seconds for one overlay; ``end=None`` means whole video."""
    o = dict(ov or {})
    start = parse_time_seconds(o.get("start_sec"))
    start = max(0.0, start) if start is not None else 0.0
    end = parse_time_seconds(o.get("end_sec"))
    return start, (max(0.0, end) if end is not None else None)


def overlay_visible_at(ov: dict[str, Any] | None, seconds: float) -> bool:
    """True when the overlay's start/end window contains ``seconds`` (inclusive)."""
    start, end = overlay_time_window(ov)
    try:
        current = float(seconds)
    except (TypeError, ValueError):
        return False
    return current >= start and (end is None or current <= end)


def overlay_enable_expr(ov: dict[str, Any] | None) -> str:
    """FFmpeg ``enable`` expression for one overlay (empty for whole video)."""
    start, end = overlay_time_window(ov)
    if end is None:
        return "" if start <= 0.0 else f"gte(t,{start:.3f})"
    return f"between(t,{start:.3f},{end:.3f})"


def overlay_enable_option(ov: dict[str, Any] | None) -> str:
    """Complete ``:enable='...'`` suffix, or ``''`` for a whole-video overlay."""
    expression = overlay_enable_expr(ov)
    return f":enable='{expression}'" if expression else ""


def overlay_pixel_geometry(ov: dict[str, Any], video_w: int, video_h: int) -> tuple[int, int, int]:
    """(x, y, width) in pixels for one overlay against the given frame size.

    Position is clamped so the image stays visible even at extreme percentages.
    """
    o = normalize_overlay(ov)
    w = max(8, int(round(o["width_percent"] * video_w)))
    x = int(round(o["x_percent"] * video_w))
    y = int(round(o["y_percent"] * video_h))
    x = max(0, min(x, video_w - w))     # keep the whole image inside the frame
    y = max(0, min(y, video_h - 8))     # height is unknown (aspect kept): only the top edge matters
    return x, y, w


def build_overlay_filters(overlays: list[dict[str, Any]] | None, video_w: int, video_h: int,
                          base_label: str = "[0:v]", first_input_index: int = 1
                          ) -> tuple[str, list[str], str, list[int]]:
    """Image overlay chain (spec §4.5).

    Returns (filter_complex_part, input_files, out_label, input_indices) where
    filter_complex_part contains scale/format/colorchannelmixer + overlay stages.
    Disabled overlays and files that do not exist are skipped by the caller
    (this builder is pure and testable without a filesystem).
    """
    parts: list[str] = []
    inputs: list[str] = []
    indices: list[int] = []
    current = base_label
    counter = 0
    for raw in (overlays or [])[:MAX_CUSTOM_OVERLAYS]:
        o = normalize_overlay(raw)
        if not o["enabled"] or not o["path"]:
            continue
        x, y, w = overlay_pixel_geometry(o, video_w, video_h)
        idx = first_input_index + len(inputs)
        inputs.append(o["path"])
        indices.append(idx)
        mid, out = f"ov{counter}", f"ovout{counter}"
        parts.append(
            f"[{idx}:v]scale={w}:-1,format=rgba,colorchannelmixer=aa={o['opacity']:.2f}[{mid}]"
        )
        parts.append(
            f"{current}[{mid}]overlay=x={x}:y={y}:eof_action=repeat{overlay_enable_option(o)}[{out}]"
        )
        current = f"[{out}]"
        counter += 1
    if not parts:
        return "", [], current, []
    return ";".join(parts), inputs, current, indices


@dataclass
class FFmpegFilterChain:
    """Builds the complete video filter chain in the spec §11.1 order.

    ``add_*`` methods must be called in the documented order. ``build()``
    returns (filter_complex, additional_inputs): the linear steps run on
    ``[0:v]`` first, then each image overlay is scaled and composited.
    Smart Sync / scale / ratio-pad steps live in core.py's existing graphs, so
    this class covers the steps that surround them.
    """

    video_w: int
    video_h: int
    filters: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    overlays: list[dict[str, Any]] = field(default_factory=list)

    def add_crop(self, crop: dict[str, Any] | None) -> bool:
        """Step 1: crop. Returns True when a crop filter was added (dimensions updated)."""
        f = build_crop_filter(crop, self.video_w, self.video_h)
        if f:
            self.filters.append(f)
            self.steps.append("crop")
            self.video_w, self.video_h = crop_size(crop, self.video_w, self.video_h)
            return True
        return False

    def add_color(self, cc: dict[str, Any] | None) -> bool:
        """Step 2: color correction (eq)."""
        f = build_color_filter(cc)
        if f:
            self.filters.append(f)
            self.steps.append("color")
            return True
        return False

    def add_zoom_mirror(self, zoom: float, mirror: bool) -> None:
        """Step 4: mirror + crop-zoom (scale up, crop the center back)."""
        zoom = max(1.0, min(float(zoom), 1.25))
        if mirror:
            self.filters.append("hflip")
            if "zoom_mirror" not in self.steps:
                self.steps.append("zoom_mirror")
        if zoom > 1.001:
            zw, zh = _even(self.video_w * zoom), _even(self.video_h * zoom)
            self.filters.append(f"scale={zw}:{zh}")
            x_off, y_off = (zw - self.video_w) // 2, (zh - self.video_h) // 2
            self.filters.append(f"crop={self.video_w}:{self.video_h}:{x_off}:{y_off}")
            if "zoom_mirror" not in self.steps:
                self.steps.append("zoom_mirror")

    def add_overlay(self, image_path: str, x_pct: float, y_pct: float, w_pct: float, opacity: float,
                    start_sec: float | None = None, end_sec: float | None = None) -> None:
        """Step 7: record one image overlay (composited by build()).

        ``start_sec``/``end_sec`` accept seconds or ``MM:SS`` / ``H:MM:SS``;
        blank end means the image stays for the whole video.
        """
        self.overlays.append(normalize_overlay({
            "path": image_path, "x_percent": x_pct, "y_percent": y_pct,
            "width_percent": w_pct, "opacity": opacity, "enabled": True,
            "start_sec": start_sec, "end_sec": end_sec,
        }))
        if "overlays" not in self.steps:
            self.steps.append("overlays")

    def build(self) -> tuple[str, list[str]]:
        """(filter_complex, additional_input_files). The output stream is labeled [vchain]."""
        linear = self.build_linear()
        active = [o for o in self.overlays if o.get("enabled", True) and o.get("path")]
        if not active:
            graph = f"[0:v]{linear}[vchain]" if linear else "[0:v]null[vchain]"
            return graph, []
        base = f"[0:v]{linear}[vhead];" if linear else ""
        running = "[vhead]" if linear else "[0:v]"
        stages: list[str] = []
        inputs: list[str] = []
        for i, ov in enumerate(active):
            x, y, w = overlay_pixel_geometry(ov, self.video_w, self.video_h)
            idx = 1 + len(inputs)
            inputs.append(ov["path"])
            stages.append(
                f"[{idx}:v]scale={w}:-1,format=rgba,colorchannelmixer=aa={ov['opacity']:.2f}[ov{i}]"
            )
            stages.append(
                f"{running}[ov{i}]overlay=x={x}:y={y}:eof_action=repeat"
                f"{overlay_enable_option(ov)}[ovout{i}]"
            )
            running = f"[ovout{i}]"
        stages.append(f"{running}null[vchain]")
        return base + ";".join(stages), inputs

    def build_linear(self) -> str:
        return ",".join(self.filters)


def build_prefilter(crop: dict[str, Any] | None, cc: dict[str, Any] | None,
                    video_w: int, video_h: int) -> tuple[str, tuple[int, int]]:
    """The crop+color prefix every Smart Sync part graph shares (steps 1-2).

    Returns (filter_csv_without_trailing_comma, effective_size_after_crop).
    """
    chain = FFmpegFilterChain(video_w=video_w, video_h=video_h)
    chain.add_crop(crop)
    chain.add_color(cc)
    return chain.build_linear(), (chain.video_w, chain.video_h)


def build_ducking_filter(segments: list[dict[str, Any]], duck_volume: float, fade_ms: int = 200) -> str | None:
    """FFmpeg volume expression that ducks the original audio during narration (spec §8.3).

    segments: [{"audio_start": float, "audio_end": float}, ...] on the output timeline.
    duck_volume: 0.0..1.0 target volume while the narrator speaks.

    Each segment gets a linear 200 ms ramp in and out. The per-segment ramp is
    max(0, min(1, min((t-start)/fade, (end-t)/fade))); the final gain is
    ``1 - (1 - duck) * max(all ramps)``, so overlapping windows never over-duck
    and the volume returns smoothly to 1.0 in every pause.
    """
    duck = max(0.0, min(1.0, float(duck_volume)))
    if duck >= 0.999:
        return None
    fade_s = max(0.0, float(fade_ms) / 1000.0)
    ramps: list[str] = []
    for seg in segments or []:
        try:
            start = float(seg.get("audio_start", seg.get("start", 0.0)))
            end = float(seg.get("audio_end", seg.get("end", 0.0)))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        # A window shorter than the requested fade cannot reach the full duck
        # level; shrink the ramp so it still forms a clean triangle.
        fade = min(fade_s, (end - start) / 2.0) if fade_s > 0 else 0.0
        if fade <= 0.0001:
            ramps.append(f"between(t,{start:.3f},{end:.3f})")
            continue
        ramps.append(
            f"max(0,min(1,min((t-{start:.3f})/{fade:.3f},({end:.3f}-t)/{fade:.3f})))"
        )
    if not ramps:
        return None
    if len(ramps) == 1:
        ramp_expr = ramps[0]
    else:
        ramp_expr = "max(" + ",".join(ramps) + ")"
    expr = f"1-{1.0 - duck:.3f}*{ramp_expr}"
    return f"volume='{expr}':eval=frame"
