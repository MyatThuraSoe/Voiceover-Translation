from __future__ import annotations

import subprocess
from pathlib import Path


def probe_duration(ffprobe: str, source: str) -> float:
    # Preview follows the primary video stream, not container/audio-tail duration.
    p = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', source],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if p.returncode == 0:
        for line in (p.stdout or '').strip().splitlines():
            try:
                d = float(line.strip())
                if d > 0.05:
                    return d
            except Exception:
                pass
    p2 = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', source],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if p2.returncode != 0:
        raise RuntimeError((p2.stderr or p.stderr)[-1200:] or 'ffprobe failed')
    return float(p2.stdout.strip())


def extract_preview_frame(
    ffmpeg: str,
    source: str,
    timestamp: float,
    zoom: float,
    mirror: bool,
    output: str,
    width: int = 720,
    height: int = 405,
    ratio: str = "16:9",
    fill: str = "blur",
    source_size: tuple[int, int] = (0, 0),
    prefilter: str = "",
    blur_boxes: list[dict] | None = None,
    blur_strength: float = 18,
    frame_size: tuple[int, int] = (1920, 1080),
) -> str:
    """One frame drawn exactly like the render (same flip/zoom/ratio/fill filters), at preview size.

    ``prefilter`` is the V2 crop + color-correction prefix (spec §11.1 steps 1-2),
    applied before the visual filters so the preview matches the final render.
    ``blur_boxes`` are applied with the same crop + boxblur chain as the final
    overlay pass, scaled to the preview canvas.
    """
    from core import _visual_filters, frame_layout

    layout = frame_layout(ratio, fill, source_size[0], source_size[1], canvas=(width, height))
    vf, _zoom = _visual_filters(mirror, zoom, layout)
    if prefilter:
        vf = f"{prefilter},{vf}"

    boxes = [b for b in (blur_boxes or []) if isinstance(b, dict)]
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', f'{max(0.0, float(timestamp)):.3f}', '-i', source,
        '-frames:v', '1',
    ]
    if not boxes:
        cmd += ['-vf', vf, str(out)]
    else:
        fw, fh = max(1, int(frame_size[0])), max(1, int(frame_size[1]))
        radius = max(1, int(round(max(1.0, float(blur_strength)) * width / fw)))
        chroma_radius = min(9, radius)
        graph: list[str] = []
        current = '[0:v]'
        if vf:
            graph.append(f'[0:v]{vf}[vpre]')
            current = '[vpre]'
        for i, box in enumerate(boxes):
            x = min(width - 8, max(0, int(round(float(box.get('x', 0)) * width))))
            y = min(height - 8, max(0, int(round(float(box.get('y', 0)) * height))))
            w = max(8, int(round(float(box.get('w', 0.1)) * width)))
            h = max(8, int(round(float(box.get('h', 0.1)) * height)))
            w, h = min(w, width - x), min(h, height - y)
            base, crop, blur, result = f'pbase{i}', f'pcrop{i}', f'pblur{i}', f'pv{i}'
            graph.append(f'{current}split=2[{base}][{crop}]')
            graph.append(
                f'[{crop}]crop={w}:{h}:{x}:{y},'
                f'boxblur=luma_radius={radius}:luma_power=2:'
                f'chroma_radius={chroma_radius}:chroma_power=1[{blur}]'
            )
            graph.append(f'[{base}][{blur}]overlay={x}:{y}[{result}]')
            current = f'[{result}]'
        cmd += ['-filter_complex', ';'.join(graph), '-map', current, str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != 0 or not out.is_file():
        raise RuntimeError(p.stderr[-1600:] or 'preview extraction failed')
    return str(out)


def scale_logo_preview(ffmpeg: str, source: str, width_px: int, output: str) -> str:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', source,
        '-frames:v', '1', '-vf', f'scale={max(24, int(width_px))}:-1', str(out),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != 0 or not out.is_file():
        raise RuntimeError(p.stderr[-1600:] or 'logo preview scale failed')
    return str(out)
