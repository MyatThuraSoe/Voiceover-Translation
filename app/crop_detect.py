"""Video cropping (V2, 2026-11): automatic black-bar detection via ffmpeg cropdetect.

The detector samples the first N seconds, takes the MEDIAN of every reported
crop window (cropdetect prints one line per frame) and converts the result to
resolution-independent percentages, so a detected crop can be re-applied to any
re-encode of the same footage (spec §5.4).
"""
from __future__ import annotations

import re
import statistics
import subprocess
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def probe_video_dimensions(ffprobe_path: str, video_path: str | Path) -> tuple[int, int]:
    """(width, height) of the first video stream (csv probe; rotation-aware callers may pre-rotate)."""
    cmd = [
        str(ffprobe_path), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    if result.returncode != 0 or not (result.stdout or "").strip():
        raise RuntimeError(f"ffprobe failed: {(result.stderr or '')[-800:]}")
    try:
        video_w, video_h = map(int, (result.stdout.strip().split(",") + ["0", "0"])[:2])
    except ValueError as exc:
        raise RuntimeError(f"ffprobe gave no usable size: {result.stdout!r}") from exc
    if video_w <= 0 or video_h <= 0:
        raise RuntimeError(f"ffprobe gave no usable size: {result.stdout!r}")
    return video_w, video_h


def parse_cropdetect_output(stderr_text: str) -> list[tuple[int, int, int, int]]:
    """All crop=W:H:X:Y readings from a cropdetect run: [(w, h, x, y), ...]."""
    return [(int(w), int(h), int(x), int(y)) for w, h, x, y in _CROP_RE.findall(stderr_text)]


def median_crop(crops: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    """Most stable reading across frames: the median of every dimension."""
    if not crops:
        raise ValueError("no cropdetect readings")
    return (
        int(statistics.median([c[0] for c in crops])),
        int(statistics.median([c[1] for c in crops])),
        int(statistics.median([c[2] for c in crops])),
        int(statistics.median([c[3] for c in crops])),
    )


def crop_to_percent(w: int, h: int, x: int, y: int, video_w: int, video_h: int) -> dict[str, float]:
    return {
        "x_percent": x / video_w,
        "y_percent": y / video_h,
        "width_percent": w / video_w,
        "height_percent": h / video_h,
    }


def detect_black_bars(
    ffprobe_path: str,
    ffmpeg_path: str,
    video_path: str | Path,
    duration_sample: float = 10.0,
    threshold: int = 24,
) -> dict | None:
    """Run ffmpeg cropdetect on the video and parse the result (spec §5.4).

    Returns {"x_percent", "y_percent", "width_percent", "height_percent"} in
    0..1 fractions, or None when detection fails / the frame has no black bars.
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(f"Video not found: {video_path}")
    video_w, video_h = probe_video_dimensions(ffprobe_path, video_path)
    duration_sample = max(1.0, float(duration_sample))

    # cropdetect=limit:round:reset — reset=0 keeps one accumulated reading,
    # every frame still prints the current best window.
    detect_cmd = [
        str(ffmpeg_path), "-hide_banner",
        "-i", str(video_path),
        "-vf", f"cropdetect={int(threshold)}:16:0",
        "-t", f"{duration_sample:.2f}",
        "-f", "null", "-",
    ]
    result = subprocess.run(detect_cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    crops = parse_cropdetect_output(result.stderr or "")
    if not crops:
        return None
    med_w, med_h, med_x, med_y = median_crop(crops)
    if med_w >= video_w and med_h >= video_h:
        return None  # full frame: no black bars
    if med_w < 16 or med_h < 16:
        return None  # degenerate detection
    return crop_to_percent(med_w, med_h, med_x, med_y, video_w, video_h)


def crop_is_full_frame(crop: dict | None) -> bool:
    """True when the crop window covers (nearly) the whole frame."""
    if not crop or not crop.get("enabled", False):
        return True
    w = float(crop.get("width_percent", 1.0) or 1.0)
    h = float(crop.get("height_percent", 1.0) or 1.0)
    return w >= 0.999 and h >= 0.999
