"""Dedicated Two-Ratio tab: two editable previews, two styles, one job.

Video A and Video B each own a full style profile (ratio, fill, text sizes,
positions, subtitles, logo, crop, colour, blur boxes, custom overlays and
ducking). Each side has its own Recap-style preview, so positions can be dragged
and timed image overlays can be checked before the job starts. The shared
source / language / voice settings are the same Tk variables used by the Recap
tab, so there is still only one script and one narration voice for both videos.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from app_paths import app_root, data_root
from core import OUTPUT_RATIOS, QUALITY_LABELS, output_canvas, probe_video_size
from filter_chain import (ASPECT_LOCKS, MAX_CUSTOM_OVERLAYS, build_prefilter, format_time_seconds,
                          normalize_color, normalize_crop, normalize_overlay, overlay_visible_at,
                          parse_time_seconds)
from preview_tools import extract_preview_frame, probe_duration, scale_logo_preview
from recap_planner import SOURCE_LANGUAGES
from recap_subtitles import SubtitleControls
from render_variants import canonical_ratio, default_profile_for_ratio, prepare_two_ratio_settings, profile_from_settings, ratio_key
from subtitle_fonts import latin_fonts, myanmar_fonts
from subtitle_png import geometry as subtitle_geometry, sample as subtitle_sample
from tts_engine import ENGINE_LABELS

LANGUAGE_LABELS = {"auto": "Auto-detect", **SOURCE_LANGUAGES}
TARGET_LABELS = {"mm": "Myanmar  ·  translate", "en": "English  ·  rewrite"}
FILL_LABELS = {"blur": "Blur background", "black": "Black bars", "crop": "Crop to fill"}
ASPECT_OPTIONS = tuple(ASPECT_LOCKS.keys())

ROOT_DIR = app_root()
PREVIEW_DIR = data_root() / "logs" / "two_ratio_previews"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def _as_float(value: object, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_int(value: object, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _tool_path(cfg: dict, name: str) -> str:
    """ffmpeg / ffprobe: config path, bundled folder, PATH, then C:\\ffmpeg\\bin."""
    for candidate in (str(cfg.get(f"{name}_path") or ""), str(ROOT_DIR / "ffmpeg" / f"{name}.exe"),
                      str(ROOT_DIR / "ffmpeg" / "bin" / f"{name}.exe"), shutil.which(name) or "",
                      rf"C:\ffmpeg\bin\{name}.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    return name


def _fmt_rate(value: object) -> str:
    return f"{int(round(_as_float(value))):+d}%"


def _fmt_pitch(value: object) -> str:
    return f"{int(round(_as_float(value))):+d}Hz"


class StylePanel(ttk.Frame):
    """All style controls and per-video overlays for one output video."""

    def __init__(self, master, *, app, name: str, ratio: str, on_change, letter: str = "A") -> None:
        super().__init__(master, padding=(8, 6))
        self.app = app
        self.name = name
        self.letter = letter
        self.on_change = on_change
        self.preview: RatioPreview | None = None
        self._loading = False
        self._labels: list[tuple[tk.Variable, ttk.Label, str]] = []

        self.ratio = tk.StringVar(value=ratio)
        self.fill = tk.StringVar(value=FILL_LABELS["blur"])
        self.quality = tk.StringVar(value=QUALITY_LABELS["1080"])
        self.zoom = tk.DoubleVar(value=1.0)
        self.mirror = tk.BooleanVar(value=False)
        self.title_text = tk.StringVar(value="")
        self.title_size = tk.IntVar(value=54)
        self.title_pos = [0.50, 0.12]
        self.title_pos_vars = [tk.StringVar(value="0.50"), tk.StringVar(value="0.12")]
        self.logo_enabled = tk.BooleanVar(value=False)
        self.logo_path = tk.StringVar(value="")
        self.logo_size = tk.DoubleVar(value=0.16)
        self.logo_opacity = tk.DoubleVar(value=0.65)
        self.logo_pos = [0.80, 0.06]
        self.logo_pos_vars = [tk.StringVar(value="0.80"), tk.StringVar(value="0.06")]
        self.extra_text = tk.StringVar(value="")
        self.extra_size = tk.IntVar(value=42)
        self.extra_opacity = tk.DoubleVar(value=0.65)
        self.extra_pos = [0.50, 0.84]
        self.extra_pos_vars = [tk.StringVar(value="0.50"), tk.StringVar(value="0.84")]

        self.blur_boxes: list[dict[str, float]] = []
        self.selected_blur: int | None = None
        self.custom_overlays: list[dict] = []
        self.selected_overlay: int | None = None
        self._overlay_enabled_vars: list[tk.BooleanVar] = []
        self._overlay_time_vars: list[tuple[tk.StringVar, tk.StringVar]] = []
        self._overlay_window_labels: list[ttk.Label] = []

        self.crop_enabled = tk.BooleanVar(value=False)
        self.crop_x = tk.DoubleVar(value=0.0)
        self.crop_y = tk.DoubleVar(value=0.0)
        self.crop_w = tk.DoubleVar(value=1.0)
        self.crop_h = tk.DoubleVar(value=1.0)
        self.crop_aspect = tk.StringVar(value="free")
        self.color_enabled = tk.BooleanVar(value=False)
        self.color_brightness = tk.DoubleVar(value=0.0)
        self.color_contrast = tk.DoubleVar(value=1.0)
        self.color_saturation = tk.DoubleVar(value=1.0)
        self.duck_enabled = tk.BooleanVar(value=False)
        self.duck_level = tk.IntVar(value=30)

        self.columnconfigure(0, weight=1)
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        ttk.Label(self, text=self.name, font=("Segoe UI Semibold", 12)).grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="Drag items on the preview above", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w")
        ttk.Separator(self).grid(row=2, column=0, sticky="ew", pady=(4, 8))

        row = 3
        video = ttk.LabelFrame(self, text="Video", padding=(6, 4))
        video.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        video.columnconfigure(1, weight=1)
        ttk.Label(video, text="Ratio").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Combobox(video, textvariable=self.ratio, values=OUTPUT_RATIOS, state="readonly",
                     width=10).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(video, text="Fill").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(video, textvariable=self.fill, values=tuple(FILL_LABELS.values()),
                     state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(video, text="Quality").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(video, textvariable=self.quality, values=tuple(QUALITY_LABELS.values()),
                     state="readonly").grid(row=2, column=1, sticky="ew", pady=3)
        self._slider(video, 3, "Zoom", 1.00, 1.25, self.zoom, "{:.2f}x")
        ttk.Checkbutton(video, text="Flip video (mirror)", variable=self.mirror,
                        command=self._changed).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        blur = ttk.LabelFrame(self, text="Blur Watermark Area", padding=(6, 4))
        blur.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        blur_btns = ttk.Frame(blur)
        blur_btns.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Button(blur_btns, text="+ Draw Blur Box", style="Compact.TButton",
                   command=self._begin_blur_box).pack(side="left")
        ttk.Button(blur_btns, text="Delete Selected", style="Compact.TButton",
                   command=self._delete_selected_blur).pack(side="left", padx=(6, 0))
        self.blur_count_label = ttk.Label(blur_btns, text="0 box", style="Muted.TLabel")
        self.blur_count_label.pack(side="left", padx=(8, 0))
        ttk.Label(blur, text="Click + drag on this video's preview to mark a watermark",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        row += 1
        title = ttk.LabelFrame(self, text="Title", padding=(6, 4))
        title.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        title.columnconfigure(0, weight=1)
        ttk.Entry(title, textvariable=self.title_text).grid(row=0, column=0, sticky="ew", pady=3)
        self._number(title, 1, "Size", self.title_size, 24, 96)
        self._position(title, 2, self.title_pos, self.title_pos_vars)

        row += 1
        extra = ttk.LabelFrame(self, text="Extra Text", padding=(6, 4))
        extra.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        extra.columnconfigure(0, weight=1)
        ttk.Entry(extra, textvariable=self.extra_text).grid(row=0, column=0, sticky="ew", pady=3)
        self._number(extra, 1, "Size", self.extra_size, 18, 84)
        self._slider(extra, 2, "Opacity", 0.10, 1.00, self.extra_opacity, "{:.0%}")
        self._position(extra, 3, self.extra_pos, self.extra_pos_vars)

        row += 1
        logo = ttk.LabelFrame(self, text="Logo", padding=(6, 4))
        logo.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        logo.columnconfigure(0, weight=1)
        ttk.Checkbutton(logo, text="Enable logo", variable=self.logo_enabled,
                        command=self._changed).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(logo, textvariable=self.logo_path).grid(row=1, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(logo, text="Browse", style="Compact.TButton",
                   command=self._browse_logo).grid(row=1, column=2, padx=(6, 0))
        self._slider(logo, 2, "Width", 0.06, 0.35, self.logo_size, "{:.0%}")
        self._slider(logo, 3, "Opacity", 0.10, 1.00, self.logo_opacity, "{:.0%}")
        self._position(logo, 4, self.logo_pos, self.logo_pos_vars)

        row += 1
        overlay_box = ttk.LabelFrame(self, text="Custom Image Overlays (timed)", padding=(6, 4))
        overlay_box.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        overlay_box.columnconfigure(0, weight=1)
        ov_head = ttk.Frame(overlay_box)
        ov_head.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Button(ov_head, text="+ Add Image", style="Compact.TButton",
                   command=self._add_custom_overlay).pack(side="left")
        self.overlay_count_label = ttk.Label(ov_head, text="0 / 5", style="Muted.TLabel")
        self.overlay_count_label.pack(side="left", padx=(8, 0))
        self.overlays_host = ttk.Frame(overlay_box)
        self.overlays_host.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        self.overlays_host.columnconfigure(0, weight=1)
        self._rebuild_overlay_rows()

        row += 1
        ttk.Label(self, text="Narration Subtitle", font=("Segoe UI Semibold", 10)).grid(
            row=row, column=0, sticky="w")
        row += 1
        self.subtitles = SubtitleControls(self, self.app.cfg.get("subtitles"), self._changed)
        families = latin_fonts() if self.app._target_code() == "en" else list(myanmar_fonts())
        self.subtitles.set_font_choices(families)
        self.subtitles.grid(row=row, column=0, sticky="ew", pady=(4, 8))

        row += 1
        advanced = ttk.LabelFrame(self, text="Advanced — independent for this video", padding=(6, 4))
        advanced.grid(row=row, column=0, sticky="ew")
        advanced.columnconfigure(1, weight=1)
        ttk.Checkbutton(advanced, text="Enable crop", variable=self.crop_enabled,
                        command=self._changed).grid(row=0, column=0, columnspan=3, sticky="w")
        crop_btns = ttk.Frame(advanced)
        crop_btns.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(crop_btns, text="Auto-detect black bars", style="Compact.TButton",
                   command=self._auto_detect_crop).pack(side="left")
        ttk.Button(crop_btns, text="Reset (full frame)", style="Compact.TButton",
                   command=self._reset_crop).pack(side="left", padx=(6, 0))
        ttk.Label(crop_btns, text="Lock").pack(side="left", padx=(8, 4))
        aspect_combo = ttk.Combobox(crop_btns, textvariable=self.crop_aspect, values=ASPECT_OPTIONS,
                                    state="readonly", width=8)
        aspect_combo.bind("<<ComboboxSelected>>", lambda _e: self._changed())
        aspect_combo.pack(side="left")
        for r, (text, var, low, high) in enumerate((
                ("Crop X", self.crop_x, 0.0, 1.0), ("Crop Y", self.crop_y, 0.0, 1.0),
                ("Crop W", self.crop_w, 0.02, 1.0), ("Crop H", self.crop_h, 0.02, 1.0)), start=2):
            self._slider(advanced, r, text, low, high, var, "{:.2f}")
        ttk.Separator(advanced).grid(row=6, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Checkbutton(advanced, text="Enable colour correction", variable=self.color_enabled,
                        command=self._changed).grid(row=7, column=0, columnspan=3, sticky="w")
        self._slider(advanced, 8, "Brightness", -0.5, 0.5, self.color_brightness, "{:+.2f}")
        self._slider(advanced, 9, "Contrast", 0.5, 2.0, self.color_contrast, "{:.2f}")
        self._slider(advanced, 10, "Saturation", 0.0, 3.0, self.color_saturation, "{:.2f}")
        ttk.Button(advanced, text="Reset colour", style="Compact.TButton",
                   command=self._reset_color).grid(row=11, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Separator(advanced).grid(row=12, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Checkbutton(advanced, text="Duck background audio", variable=self.duck_enabled,
                        command=self._changed).grid(row=13, column=0, columnspan=3, sticky="w")
        self._number(advanced, 14, "Duck %", self.duck_level, 10, 80)

        for var in (self.ratio, self.fill, self.quality, self.title_text, self.title_size,
                    self.extra_text, self.extra_size, self.extra_opacity, self.logo_path,
                    self.logo_size, self.logo_opacity, self.crop_aspect):
            var.trace_add("write", lambda *_a: self._changed())
        self._format_labels()
        self._update_blur_count()

    def _slider(self, parent, row: int, label: str, low: float, high: float,
                variable: tk.Variable, fmt: str) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=2)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label, width=10).grid(row=0, column=0, sticky="w")
        ttk.Scale(frame, from_=low, to=high, orient="horizontal", variable=variable,
                  command=lambda _v: self._changed()).grid(row=0, column=1, sticky="ew")
        value = ttk.Label(frame, width=8, anchor="e")
        value.grid(row=0, column=2, padx=(6, 0))
        self._labels.append((variable, value, fmt))

    def _number(self, parent, row: int, label: str, variable: tk.Variable,
                low: float, high: float) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=2)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label, width=10).grid(row=0, column=0, sticky="w")
        spin = ttk.Spinbox(frame, from_=low, to=high, width=8, textvariable=variable,
                           command=self._changed)
        spin.grid(row=0, column=1, sticky="w")
        for event in ("<Return>", "<FocusOut>"):
            spin.bind(event, lambda _e: self._changed())

    def _position(self, parent, row: int, pos: list[float], vars_: list[tk.StringVar]) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=2)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        ttk.Label(frame, text="Position", width=10).grid(row=0, column=0, sticky="w")
        for index, column in ((0, 1), (1, 3)):
            ttk.Label(frame, text="X" if index == 0 else "Y").grid(row=0, column=column - 1 if index == 0 else 2,
                                                                   sticky="e")
            spin = ttk.Spinbox(frame, from_=0.0, to=1.0, increment=0.01, width=7,
                               textvariable=vars_[index])
            spin.grid(row=0, column=column, sticky="w")
            spin.bind("<Return>", lambda _e, i=index: self._set_position(pos, vars_, i))
            spin.bind("<FocusOut>", lambda _e, i=index: self._set_position(pos, vars_, i))

    def _set_position(self, pos: list[float], vars_: list[tk.StringVar], index: int) -> None:
        try:
            pos[index] = _clamp(float(vars_[index].get()), 0.0, 1.0)
        except (ValueError, tk.TclError):
            vars_[index].set(f"{pos[index]:.2f}")
            return
        vars_[index].set(f"{pos[index]:.2f}")
        self._changed()

    def sync_position_vars(self) -> None:
        """Refresh X/Y spinboxes after a preview drag."""
        for pos, vars_ in ((self.title_pos, self.title_pos_vars), (self.logo_pos, self.logo_pos_vars),
                           (self.extra_pos, self.extra_pos_vars)):
            for index in (0, 1):
                if self._loading:
                    vars_[index].set(f"{pos[index]:.2f}")
                else:
                    vars_[index].set(f"{pos[index]:.2f}")

    def _format_labels(self) -> None:
        try:
            if not self.winfo_exists():
                return
            for variable, label, fmt in self._labels:
                try:
                    label.configure(text=fmt.format(float(variable.get())))
                except (ValueError, tk.TclError):
                    pass
            self.after(300, self._format_labels)
        except tk.TclError:
            return

    def _changed(self) -> None:
        if not self._loading:
            self.on_change()

    def _browse_logo(self) -> None:
        path = filedialog.askopenfilename(title=f"Logo for {self.name}",
                                          filetypes=[("Image", "*.png *.webp *.jpg *.jpeg"), ("All", "*.*")])
        if path:
            self.logo_path.set(path)
            self.logo_enabled.set(True)
            self._changed()

    # --------------------------------------------------------------- blur boxes
    def _update_blur_count(self) -> None:
        label = getattr(self, "blur_count_label", None)
        if label is not None:
            count = len(self.blur_boxes)
            label.configure(text=f"{count} box" + ("" if count == 1 else "es"))

    def _begin_blur_box(self) -> None:
        if self.preview is not None:
            self.preview.begin_blur_box()

    def _delete_selected_blur(self) -> None:
        if self.selected_blur is None or not (0 <= self.selected_blur < len(self.blur_boxes)):
            return
        self.blur_boxes.pop(self.selected_blur)
        self.selected_blur = None
        self._update_blur_count()
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    # ---------------------------------------------------------- image overlays
    def _add_custom_overlay(self) -> None:
        if len(self.custom_overlays) >= MAX_CUSTOM_OVERLAYS:
            messagebox.showinfo("Custom Overlays", f"Maximum {MAX_CUSTOM_OVERLAYS} overlays.", parent=self)
            return
        path = filedialog.askopenfilename(title=f"Overlay image for {self.name}",
                                          filetypes=[("Image", "*.png *.jpg *.jpeg *.webp"), ("All", "*.*")])
        if not path:
            return
        self.custom_overlays.append(normalize_overlay({
            "id": len(self.custom_overlays) + 1, "path": path,
            "x_percent": 0.82, "y_percent": 0.06, "width_percent": 0.14,
            "opacity": 0.90, "enabled": True,
        }, len(self.custom_overlays) + 1))
        self.selected_overlay = len(self.custom_overlays) - 1
        self._rebuild_overlay_rows()
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    def _browse_custom_overlay(self, index: int) -> None:
        if not (0 <= index < len(self.custom_overlays)):
            return
        path = filedialog.askopenfilename(title="Replace overlay image",
                                          filetypes=[("Image", "*.png *.jpg *.jpeg *.webp"), ("All", "*.*")])
        if path:
            self.custom_overlays[index]["path"] = path
            if self.preview is not None:
                self.preview.clear_overlay_cache()
            self._rebuild_overlay_rows()
            self._changed()
            if self.preview is not None:
                self.preview.redraw()

    def _remove_custom_overlay(self, index: int) -> None:
        if not (0 <= index < len(self.custom_overlays)):
            return
        self.custom_overlays.pop(index)
        self.selected_overlay = None
        for i, overlay in enumerate(self.custom_overlays, 1):
            overlay["id"] = i
        if self.preview is not None:
            self.preview.clear_overlay_cache()
        self._rebuild_overlay_rows()
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    def _rebuild_overlay_rows(self) -> None:
        host = getattr(self, "overlays_host", None)
        if host is None:
            return
        for child in host.winfo_children():
            child.destroy()
        self._overlay_enabled_vars = []
        self._overlay_time_vars = []
        self._overlay_window_labels = []
        if hasattr(self, "overlay_count_label"):
            self.overlay_count_label.configure(text=f"{len(self.custom_overlays)} / {MAX_CUSTOM_OVERLAYS}")
        for i, overlay in enumerate(self.custom_overlays):
            frame = ttk.LabelFrame(host, text=f"Image {i + 1}: {Path(overlay['path']).name or '(none)'}", padding=(5, 3))
            frame.grid(row=i, column=0, sticky="ew", pady=(0, 5))
            frame.columnconfigure(1, weight=1)
            enabled_var = tk.BooleanVar(value=bool(overlay["enabled"]))
            self._overlay_enabled_vars.append(enabled_var)
            ttk.Checkbutton(frame, text="Enabled", variable=enabled_var,
                            command=lambda idx=i: self._overlay_toggle(idx)).grid(row=0, column=0, sticky="w")
            ttk.Button(frame, text="Browse", style="Compact.TButton",
                       command=lambda idx=i: self._browse_custom_overlay(idx)).grid(row=0, column=1, sticky="e")
            ttk.Button(frame, text="Remove", style="Compact.TButton",
                       command=lambda idx=i: self._remove_custom_overlay(idx)).grid(row=0, column=2, padx=(4, 0))
            for r, (text, key) in enumerate((("Size", "width_percent"), ("Opacity", "opacity")), start=1):
                ttk.Label(frame, text=text, width=8).grid(row=r, column=0, sticky="w")
                value = float(overlay[key])
                label = ttk.Label(frame, text=f"{value * 100:.0f}%", width=6, anchor="e")
                ttk.Scale(frame, from_=0.02 if key == "width_percent" else 0.10, to=1.0,
                          orient="horizontal", value=value,
                          command=lambda v, idx=i, k=key, lbl=label: self._overlay_slider(idx, k, v, lbl)
                          ).grid(row=r, column=1, sticky="ew", pady=1)
                label.grid(row=r, column=2, padx=(6, 0))
            start_var = tk.StringVar(value=format_time_seconds(overlay.get("start_sec", 0.0)))
            end_var = tk.StringVar(value=format_time_seconds(overlay.get("end_sec")))
            self._overlay_time_vars.append((start_var, end_var))
            for row, text, var, which in ((3, "Start", start_var, "start"), (4, "End", end_var, "end")):
                ttk.Label(frame, text=text, width=8).grid(row=row, column=0, sticky="w", pady=(3, 0))
                entry = ttk.Entry(frame, textvariable=var, width=9)
                entry.grid(row=row, column=1, sticky="w", pady=(3, 0))
                entry.bind("<Return>", lambda _e, idx=i, k=which, v=var: self._overlay_time_finish(idx, k, v))
                entry.bind("<FocusOut>", lambda _e, idx=i, k=which, v=var: self._overlay_time_finish(idx, k, v))
                ttk.Button(frame, text="Use preview", style="Compact.TButton",
                           command=lambda idx=i, k=which, v=var: self._overlay_time_from_preview(idx, k, v)
                           ).grid(row=row, column=2, padx=(4, 0), pady=(3, 0))
                var.trace_add("write", lambda *_a, idx=i, k=which, v=var: self._overlay_time_changed(idx, k, v))
            hint = ttk.Label(frame, text="", style="Muted.TLabel")
            self._overlay_window_labels.append(hint)
            hint.grid(row=5, column=0, columnspan=3, sticky="w", pady=(3, 0))
            self._overlay_window_hint(i)
            ttk.Label(frame, text="Position: drag on the preview above", style="Muted.TLabel").grid(
                row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _overlay_toggle(self, index: int) -> None:
        if not (0 <= index < len(self.custom_overlays)) or index >= len(self._overlay_enabled_vars):
            return
        self.custom_overlays[index]["enabled"] = bool(self._overlay_enabled_vars[index].get())
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    def _overlay_slider(self, index: int, key: str, value, label: ttk.Label) -> None:
        if not (0 <= index < len(self.custom_overlays)):
            return
        self.custom_overlays[index][key] = float(value)
        label.configure(text=f"{float(value) * 100:.0f}%")
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    def _overlay_window_hint(self, index: int) -> None:
        if not (0 <= index < len(self._overlay_window_labels)) or not (0 <= index < len(self.custom_overlays)):
            return
        overlay = self.custom_overlays[index]
        start = format_time_seconds(overlay.get("start_sec", 0.0)) or "0:00"
        end = format_time_seconds(overlay.get("end_sec")) or "end of video"
        self._overlay_window_labels[index].configure(text=f"Visible {start} → {end} · blank End = whole video")

    def _overlay_time_changed(self, index: int, which: str, var: tk.StringVar) -> None:
        if not (0 <= index < len(self.custom_overlays)):
            return
        raw = str(var.get()).strip()
        parsed = parse_time_seconds(raw) if raw else (0.0 if which == "start" else None)
        if raw and parsed is None:
            self._overlay_window_hint(index)
            return
        self.custom_overlays[index]["start_sec" if which == "start" else "end_sec"] = parsed
        self._overlay_window_hint(index)
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    def _overlay_time_finish(self, index: int, which: str, var: tk.StringVar) -> None:
        raw = str(var.get()).strip()
        if not raw:
            self._overlay_time_changed(index, which, var)
            return
        parsed = parse_time_seconds(raw)
        if parsed is not None:
            var.set(format_time_seconds(parsed))

    def _overlay_time_from_preview(self, index: int, which: str, var: tk.StringVar) -> None:
        if self.preview is not None:
            var.set(format_time_seconds(self.preview.time.get()))

    def commit_overlay_times(self) -> None:
        """Validate Start/End fields before a job starts."""
        pairs = self._overlay_time_vars
        for i, overlay in enumerate(self.custom_overlays):
            pair = pairs[i] if i < len(pairs) else None
            start_raw = str(pair[0].get()).strip() if pair else format_time_seconds(overlay.get("start_sec", 0.0))
            end_raw = str(pair[1].get()).strip() if pair else format_time_seconds(overlay.get("end_sec"))
            start = parse_time_seconds(start_raw) if start_raw else 0.0
            if start is None:
                raise ValueError(f"Video {self.letter} "
                                 f"image {i + 1} Start must be MM:SS or seconds.")
            end = parse_time_seconds(end_raw) if end_raw else None
            if end_raw and end is None:
                raise ValueError(f"Video {self.letter} "
                                 f"image {i + 1} End must be MM:SS or seconds.")
            if end is not None and end <= start:
                raise ValueError(f"Video {self.letter} "
                                 f"image {i + 1} End must be after Start.")
            overlay["start_sec"] = float(start)
            overlay["end_sec"] = float(end) if end is not None else None

    # ------------------------------------------------------------------- crop
    def _fill_code(self) -> str:
        return next((code for code, label in FILL_LABELS.items() if label == self.fill.get()), "blur")

    def _crop_values(self) -> dict:
        return normalize_crop({
            "enabled": bool(self.crop_enabled.get()),
            "x_percent": float(self.crop_x.get()), "y_percent": float(self.crop_y.get()),
            "width_percent": float(self.crop_w.get()), "height_percent": float(self.crop_h.get()),
            "aspect_lock": str(self.crop_aspect.get() or "free"),
        })

    def _apply_crop_values(self, values: dict) -> None:
        crop = normalize_crop(values)
        self.crop_enabled.set(bool(crop["enabled"]))
        self.crop_x.set(crop["x_percent"])
        self.crop_y.set(crop["y_percent"])
        self.crop_w.set(crop["width_percent"])
        self.crop_h.set(crop["height_percent"])
        self.crop_aspect.set(str(crop.get("aspect_lock") or "free"))
        self._changed()
        if self.preview is not None:
            self.preview.redraw()

    def _reset_crop(self) -> None:
        self._apply_crop_values({"enabled": bool(self.crop_enabled.get()), "aspect_lock": "free"})
        self._log("↩️ Crop reset to full frame")

    def _auto_detect_crop(self) -> None:
        source = self.app.source.get().strip()
        if not source or not Path(source).is_file():
            messagebox.showinfo("Auto Crop", "Load a source video first.", parent=self)
            return
        ffmpeg = _tool_path(self.app.cfg, "ffmpeg")
        ffprobe = _tool_path(self.app.cfg, "ffprobe")
        self._log("🔍 Auto-detecting black bars (ffmpeg cropdetect)...")

        def work() -> None:
            try:
                from crop_detect import detect_black_bars
                result = detect_black_bars(ffprobe, ffmpeg, source, duration_sample=10.0)
            except Exception as exc:                       # noqa: BLE001
                result = {"error": str(exc)}
            self.after(0, lambda: self._apply_crop_detection(result))

        threading.Thread(target=work, daemon=True).start()

    def _apply_crop_detection(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("error"):
            self._log(f"⚠️ Auto crop failed: {payload['error']}")
            messagebox.showwarning("Auto Crop", str(payload["error"]), parent=self)
            return
        if not all(k in payload for k in ("x_percent", "y_percent", "width_percent", "height_percent")):
            messagebox.showinfo("Auto Crop", "No black bars detected. The full frame is kept.", parent=self)
            return
        self._apply_crop_values({"enabled": True, "mode": "auto", "aspect_lock": str(self.crop_aspect.get() or "free"),
                                 **payload})
        self._log(f"✂️ Auto crop applied: x={payload['x_percent']:.2f} y={payload['y_percent']:.2f} "
                  f"w={payload['width_percent']:.2f} h={payload['height_percent']:.2f}")

    # ------------------------------------------------------------------ colour
    def _color_values(self) -> dict:
        return normalize_color({
            "enabled": bool(self.color_enabled.get()),
            "brightness": float(self.color_brightness.get()),
            "contrast": float(self.color_contrast.get()),
            "saturation": float(self.color_saturation.get()),
        })

    def _reset_color(self) -> None:
        self.color_enabled.set(False)
        self.color_brightness.set(0.0)
        self.color_contrast.set(1.0)
        self.color_saturation.set(1.0)
        self._changed()
        if self.preview is not None:
            self.preview.redraw()
        self._log("↩️ Colour correction reset")

    def _log(self, text: str) -> None:
        try:
            self.app._log(text)
        except Exception:
            pass

    # ---------------------------------------------------------------- profile
    def load_profile(self, profile: dict) -> None:
        """Restore a saved ratio profile without writing it back while loading."""
        self._loading = True
        try:
            values = dict(profile or {})
            if not values.get("output_ratio"):
                values = default_profile_for_ratio(self.ratio.get())
            self.ratio.set(canonical_ratio(values.get("output_ratio"), self.ratio.get()))
            self.fill.set(self.app._fill_label(str(values.get("frame_fill") or "blur")))
            self.quality.set(self.app._quality_label(str(values.get("video_quality") or "1080")))
            self.zoom.set(_as_float(values.get("zoom_factor"), 1.0))
            self.mirror.set(bool(values.get("mirror_video", False)))
            self.title_text.set(str(values.get("title_text") or ""))
            self.title_size.set(_as_int(values.get("title_size"), 54))
            self.extra_text.set(str(values.get("extra_text") or ""))
            self.extra_size.set(_as_int(values.get("extra_text_size"), 42))
            self.extra_opacity.set(_as_float(values.get("extra_text_opacity"), 0.65))
            self.logo_enabled.set(bool(values.get("logo_enabled", False)))
            self.logo_path.set(str(values.get("logo_path") or ""))
            self.logo_size.set(_as_float(values.get("logo_width_frac", values.get("logo_width_fraction")), 0.16))
            self.logo_opacity.set(_as_float(values.get("logo_opacity"), 0.65))

            for attr, key, fallback in (
                    (self.title_pos, "title_position", [0.5, 0.12]),
                    (self.logo_pos, "logo_position", [0.8, 0.06]),
                    (self.extra_pos, "extra_text_position", [0.5, 0.84])):
                raw = list(values.get(key) or fallback)[:2]
                attr[:] = [_clamp(_as_float(v, fallback[i]), 0.0, 1.0) for i, v in enumerate(raw)]
            self.sync_position_vars()

            crop = dict(self.app._crop_values())
            crop.update(dict(values.get("crop") or {}))
            color = dict(self.app._color_values())
            color.update(dict(values.get("color_correction") or {}))
            duck = dict(self.app._ducking_values())
            duck.update(dict(values.get("audio_ducking") or {}))
            self.crop_enabled.set(bool(crop.get("enabled", False)))
            self.crop_x.set(_as_float(crop.get("x_percent"), 0.0))
            self.crop_y.set(_as_float(crop.get("y_percent"), 0.0))
            self.crop_w.set(_as_float(crop.get("width_percent"), 1.0))
            self.crop_h.set(_as_float(crop.get("height_percent"), 1.0))
            self.crop_aspect.set(str(crop.get("aspect_lock") or "free"))
            self.color_enabled.set(bool(color.get("enabled", False)))
            self.color_brightness.set(_as_float(color.get("brightness"), 0.0))
            self.color_contrast.set(_as_float(color.get("contrast"), 1.0))
            self.color_saturation.set(_as_float(color.get("saturation"), 1.0))
            self.duck_enabled.set(bool(duck.get("enabled", False)))
            self.duck_level.set(_as_int(duck.get("duck_volume_percent"), 30))

            blur = [dict(item) for item in (values.get("blur_boxes") or []) if isinstance(item, dict)]
            self.blur_boxes = [{k: _as_float(v) for k, v in item.items()
                                if k in ("x", "y", "w", "h")} for item in blur]
            self.selected_blur = None
            self._update_blur_count()

            self.custom_overlays = [normalize_overlay(item, i + 1) for i, item in enumerate(
                (values.get("custom_overlays") or [])[:MAX_CUSTOM_OVERLAYS])]
            self.selected_overlay = None
            if self.preview is not None:
                self.preview.clear_overlay_cache()
            self._rebuild_overlay_rows()
            try:
                self.subtitles.load_values(values.get("subtitles") or {})
            except Exception:
                pass
            self._format_labels()
        finally:
            self._loading = False

    def profile(self) -> dict:
        """Return a complete style profile for this video."""
        overlays = {
            "title_text": self.title_text.get().strip(),
            "title_font_size": int(self.title_size.get()),
            "title_position": [float(v) for v in self.title_pos],
            "logo_enabled": bool(self.logo_enabled.get()),
            "logo_path": self.logo_path.get().strip() if self.logo_enabled.get() else "",
            "logo_width_fraction": float(self.logo_size.get()),
            "logo_opacity": float(self.logo_opacity.get()),
            "logo_position": [float(v) for v in self.logo_pos],
            "extra_text": self.extra_text.get().strip(),
            "extra_text_font_size": int(self.extra_size.get()),
            "extra_text_opacity": float(self.extra_opacity.get()),
            "extra_text_position": [float(v) for v in self.extra_pos],
            "subtitles": self.subtitles.values(),
        }
        settings = {
            "output_ratio": canonical_ratio(self.ratio.get()),
            "frame_fill": self._fill_code(),
            "video_quality": next((code for code, label in QUALITY_LABELS.items() if label == self.quality.get()), "1080"),
            "zoom_factor": float(self.zoom.get()),
            "mirror_video": bool(self.mirror.get()),
            "overlays": overlays,
            "crop": self._crop_values(),
            "color_correction": self._color_values(),
            "blur_boxes": [dict(box) for box in self.blur_boxes],
            "custom_overlays": [normalize_overlay(overlay, i + 1) for i, overlay in enumerate(self.custom_overlays)],
            "audio_ducking": {
                "enabled": bool(self.duck_enabled.get()),
                "duck_volume_percent": int(self.duck_level.get()),
            },
        }
        return profile_from_settings(settings)


class RatioPreview(ttk.Frame):
    """Recap-style frame preview for one ratio, with draggable editor items."""

    def __init__(self, master, *, app, panel: StylePanel, letter: str) -> None:
        super().__init__(master, padding=(5, 4))
        self.app = app
        self.panel = panel
        self.letter = letter
        self.duration = 0.0
        self.source_size = (0, 0)
        self.time = tk.DoubleVar(value=float(_as_float(app.cfg.get("preview_seek_seconds"), 1.0)))
        self._frame_size = (1920, 1080)
        self._canvas_size = (480, 270)
        self._photo = None
        self._logo_cache: dict[tuple, tk.PhotoImage] = {}
        self._overlay_cache: dict[tuple, tk.PhotoImage] = {}
        self._overlay_photos: list = []
        self._subtitle_key: str | None = None
        self._subtitle_path: str | None = None
        self._subtitle_busy = False
        self._subtitle_photo = None
        self._subtitle_scaled_key = None
        self._subtitle_ready: list[tuple[str, str | None]] = []
        self._refresh_job: str | None = None
        self._resize_job: str | None = None
        self.draw_blur = False
        self._draw_start: tuple[float, float] | None = None
        self._temp_rect = None
        self._drag: tuple[str, int | None, float, float, object] | None = None
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        box = ttk.LabelFrame(self, text=f"Preview {self.letter} — drag items / draw blur box", padding=(5, 4))
        box.pack(fill="both", expand=True)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(box, width=480, height=270, bg="#05070a", highlightthickness=1,
                                highlightbackground="#303744", cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.canvas.bind("<Configure>", self._resized)

        seek = ttk.Frame(box)
        seek.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        seek.columnconfigure(2, weight=1)
        ttk.Button(seek, text="◀ 5s", width=5, style="Compact.TButton",
                   command=lambda: self._seek(-5)).grid(row=0, column=0, padx=(0, 5))
        self.slider = ttk.Scale(seek, from_=0.0, to=1.0, orient="horizontal", variable=self.time,
                                command=lambda _v: self._time_changed())
        self.slider.grid(row=0, column=1, sticky="ew")
        ttk.Button(seek, text="5s ▶", width=5, style="Compact.TButton",
                   command=lambda: self._seek(5)).grid(row=0, column=2, padx=(5, 6))
        self.time_label = ttk.Label(seek, text="00:00 / 00:00", style="Muted.TLabel", width=14)
        self.time_label.grid(row=0, column=3)
        ttk.Button(seek, text="Refresh", style="Compact.TButton",
                   command=self.refresh).grid(row=0, column=4, padx=(6, 0))
        self.slider.bind("<ButtonRelease-1>", lambda _e: self.refresh())
        self.hint = ttk.Label(box, text="Load a source video to see the frame", style="Muted.TLabel")
        self.hint.grid(row=2, column=0, sticky="w", pady=(3, 0))

    # -------------------------------------------------------------- geometry
    def _bounds(self) -> tuple[int, int, int, int]:
        fw, fh = self._frame_size if self._frame_size[0] else (1920, 1080)
        cw, ch = self._canvas_size
        scale = min(cw / fw, ch / fh)
        width = max(16, int(fw * scale)) // 2 * 2
        height = max(16, int(fh * scale)) // 2 * 2
        return (cw - width) // 2, (ch - height) // 2, width, height

    def _xy(self, event) -> tuple[float, float]:
        ox, oy, width, height = self._bounds()
        return (_clamp(event.x, ox, ox + width), _clamp(event.y, oy, oy + height))

    def _resized(self, event) -> None:
        size = (event.width, event.height)
        if size == self._canvas_size:
            return
        self._canvas_size = size
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(250, self.refresh)

    def _font(self) -> str:
        return "Segoe UI" if self.app._target_code() == "en" else "Myanmar Text"

    # --------------------------------------------------------------- refresh
    def schedule_refresh(self) -> None:
        self.redraw()
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(300, self.refresh)

    def refresh(self) -> None:
        source = self.app.source.get().strip()
        if not source or not Path(source).is_file():
            self.canvas.delete("frame")
            self.canvas.delete("editor")
            self.hint.configure(text="Load a source video to see the frame")
            self._update_time_label()
            return
        try:
            ffmpeg = _tool_path(self.app.cfg, "ffmpeg")
            fw, fh = self.source_size if self.source_size[0] else (1920, 1080)
            crop = self.panel._crop_values()
            colour = self.panel._color_values()
            prefilter, effective = build_prefilter(crop, colour, fw, fh)
            ratio = canonical_ratio(self.panel.ratio.get())
            self._frame_size = output_canvas(ratio, effective[0], effective[1])
            ox, oy, width, height = self._bounds()
            out = PREVIEW_DIR / f"frame_{self.letter.lower()}.png"
            extract_preview_frame(
                ffmpeg, source, float(self.time.get()), float(self.panel.zoom.get()),
                bool(self.panel.mirror.get()), str(out), width, height,
                ratio=ratio, fill=self.panel._fill_code(), source_size=effective, prefilter=prefilter,
            )
            self._photo = tk.PhotoImage(file=str(out))
            self.canvas.delete("frame")
            self.canvas.create_image(ox, oy, image=self._photo, anchor="nw", tags=("frame",))
            self.canvas.tag_lower("frame")
            self.hint.configure(text="Ready · drag title, logo, subtitle, crop, blur or image overlay")
            self.redraw()
        except Exception as exc:                           # noqa: BLE001 — preview must never crash the app
            self.hint.configure(text=f"Preview error: {exc}")
        self._update_time_label()

    def redraw(self) -> None:
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        canvas.delete("editor")
        ox, oy, width, height = self._bounds()
        self._draw_crop(ox, oy, width, height)
        self._draw_blur_boxes(ox, oy, width, height)
        self._draw_title(ox, oy, width, height)
        self._draw_extra(ox, oy, width, height)
        self._draw_logo(ox, oy, width, height)
        self._draw_custom_overlays(ox, oy, width, height)
        self._draw_subtitle(ox, oy, width, height)

    def clear_overlay_cache(self) -> None:
        self._overlay_cache.clear()

    # ----------------------------------------------------------- frame items
    def _draw_crop(self, ox: int, oy: int, pw: int, ph: int) -> None:
        if not self.panel.crop_enabled.get():
            return
        crop = self.panel._crop_values()
        x1 = ox + crop["x_percent"] * pw
        y1 = oy + crop["y_percent"] * ph
        x2 = x1 + crop["width_percent"] * pw
        y2 = y1 + crop["height_percent"] * ph
        shade = {"fill": "#000000", "stipple": "gray50", "outline": "", "tags": ("editor", "crop_shade")}
        self.canvas.create_rectangle(ox, oy, ox + pw, y1, **shade)
        self.canvas.create_rectangle(ox, y2, ox + pw, oy + ph, **shade)
        self.canvas.create_rectangle(ox, y1, x1, y2, **shade)
        self.canvas.create_rectangle(x2, y1, ox + pw, y2, **shade)
        self.canvas.create_rectangle(x1, y1, x2, y2, outline="#7be0a4", width=2,
                                     tags=("editor", "crop", "crop_box"))
        handles = ((x1, y1), ((x1 + x2) / 2, y1), (x2, y1), (x1, (y1 + y2) / 2),
                   (x2, (y1 + y2) / 2), (x1, y2), ((x1 + x2) / 2, y2), (x2, y2))
        for i, (hx, hy) in enumerate(handles):
            self.canvas.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill="#7be0a4",
                                         outline="#08301b", tags=("editor", "crop", f"crop_handle_{i}"))
        self.canvas.create_text(x1 + 5, y1 + 6, text="CROP", fill="#7be0a4", anchor="nw",
                                font=("Segoe UI", 8, "bold"), tags=("editor", "crop"))

    def _draw_blur_boxes(self, ox: int, oy: int, pw: int, ph: int) -> None:
        self.panel._update_blur_count()
        for i, box in enumerate(self.panel.blur_boxes):
            x1 = ox + float(box["x"]) * pw
            y1 = oy + float(box["y"]) * ph
            x2 = ox + (float(box["x"]) + float(box["w"])) * pw
            y2 = oy + (float(box["y"]) + float(box["h"])) * ph
            color = "#ffd166" if i == self.panel.selected_blur else "#48d7ff"
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2, dash=(6, 4),
                                         tags=("editor", "blur", f"blur_{i}"))
            self.canvas.create_text(x1 + 4, y1 + 4, text="BLUR", fill=color, anchor="nw",
                                    font=("Segoe UI Semibold", 8), tags=("editor", f"blur_{i}"))
            if i == self.panel.selected_blur:
                self.canvas.create_rectangle(x2 - 5, y2 - 5, x2 + 5, y2 + 5, fill=color,
                                             outline="#111111", tags=("editor", f"blur_handle_{i}"))

    def _draw_title(self, ox: int, oy: int, pw: int, ph: int) -> None:
        text = self.panel.title_text.get().strip()
        if not text:
            return
        x = ox + float(self.panel.title_pos[0]) * pw
        y = oy + float(self.panel.title_pos[1]) * ph
        size = max(9, int(int(self.panel.title_size.get()) * pw / max(1, self._frame_size[0])))
        font = (self._font(), size, "bold")
        self.canvas.create_text(x + 2, y + 2, text=text, fill="#000000", anchor="center", font=font,
                                width=int(pw * 0.82), tags=("editor", "title"))
        self.canvas.create_text(x, y, text=text, fill="#ffffff", anchor="center", font=font,
                                width=int(pw * 0.82), tags=("editor", "title"))

    def _draw_extra(self, ox: int, oy: int, pw: int, ph: int) -> None:
        text = self.panel.extra_text.get().strip()
        if not text:
            return
        x = ox + float(self.panel.extra_pos[0]) * pw
        y = oy + float(self.panel.extra_pos[1]) * ph
        size = max(9, int(int(self.panel.extra_size.get()) * pw / max(1, self._frame_size[0])))
        opacity = _clamp(float(self.panel.extra_opacity.get()), 0.10, 1.0)
        shade = max(140, min(255, int(255 * opacity)))
        font = (self._font(), size, "bold")
        self.canvas.create_text(x + 2, y + 2, text=text, fill="#101010", anchor="center", font=font,
                                width=int(pw * 0.78), tags=("editor", "extra_text"))
        self.canvas.create_text(x, y, text=text, fill=f"#{shade:02x}{shade:02x}{shade:02x}",
                                anchor="center", font=font, width=int(pw * 0.78),
                                tags=("editor", "extra_text"))

    def _draw_logo(self, ox: int, oy: int, pw: int, ph: int) -> None:
        path = self.panel.logo_path.get().strip() if self.panel.logo_enabled.get() else ""
        if not path or not Path(path).is_file():
            return
        width = max(30, int(pw * float(self.panel.logo_size.get())))
        key = (path, width)
        photo = self._logo_cache.get(key)
        if photo is None:
            try:
                tmp = PREVIEW_DIR / f"logo_{self.letter.lower()}.png"
                scale_logo_preview(_tool_path(self.app.cfg, "ffmpeg"), path, width, str(tmp))
                photo = tk.PhotoImage(file=str(tmp))
                self._logo_cache[key] = photo
            except Exception:
                return
        x = ox + float(self.panel.logo_pos[0]) * pw
        y = oy + float(self.panel.logo_pos[1]) * ph
        self.canvas.create_image(x, y, image=photo, anchor="nw", tags=("editor", "logo"))

    def _draw_custom_overlays(self, ox: int, oy: int, pw: int, ph: int) -> None:
        self._overlay_photos = []
        now = float(self.time.get())
        ffmpeg = None
        for i, overlay in enumerate(self.panel.custom_overlays[:MAX_CUSTOM_OVERLAYS]):
            if not overlay.get("enabled", True) or not overlay_visible_at(overlay, now):
                continue
            x = ox + float(overlay["x_percent"]) * pw
            y = oy + float(overlay["y_percent"]) * ph
            width = max(20, int(float(overlay["width_percent"]) * pw))
            path = str(overlay.get("path") or "")
            tags = ("editor", "custom_overlay", f"custom_{i}")
            key = (i, path, width)
            photo = self._overlay_cache.get(key)
            if photo is None and path and Path(path).is_file():
                try:
                    if ffmpeg is None:
                        ffmpeg = _tool_path(self.app.cfg, "ffmpeg")
                    tmp = PREVIEW_DIR / f"overlay_{self.letter.lower()}_{i}.png"
                    scale_logo_preview(ffmpeg, path, width, str(tmp))
                    photo = tk.PhotoImage(file=str(tmp))
                    self._overlay_cache[key] = photo
                except Exception:
                    photo = None
            if photo is not None:
                self._overlay_photos.append(photo)
                self.canvas.create_image(x, y, image=photo, anchor="nw", tags=tags)
            else:
                self.canvas.create_rectangle(x, y, x + width, y + max(16, int(width * 0.25)),
                                             fill="#3a2b45", outline="#ffb3e6", tags=tags)
                self.canvas.create_text(x + 4, y + 3, text=Path(path).name[:20] or "missing image",
                                        fill="#ffd6f3", anchor="nw", font=("Segoe UI", 8), tags=tags)
            color = "#ffe066" if i == self.panel.selected_overlay else "#ff9ee6"
            self.canvas.create_rectangle(x, y, x + width, y + max(16, int(width * 0.25)),
                                         outline=color, width=2, dash=(5, 3), tags=tags)
            self.canvas.create_text(x + 3, y - 11, text=f"OV{i + 1}", fill=color, anchor="nw",
                                    font=("Segoe UI", 9, "bold"), tags=tags)

    def _draw_subtitle(self, ox: int, oy: int, pw: int, ph: int) -> None:
        try:
            values = self.panel.subtitles.values()
        except (ValueError, tk.TclError):
            return
        if not values.get("enabled"):
            return
        frame = self._frame_size
        identity = dict(values)
        identity.pop("position", None)
        key = json.dumps([identity, list(frame), self.app._target_code()], sort_keys=True)
        if key != self._subtitle_key and not self._subtitle_busy:
            self._subtitle_busy = True

            def work() -> None:
                path = None
                try:
                    folder = PREVIEW_DIR / f"subtitles_{self.letter.lower()}"
                    path = str(subtitle_sample(values, frame, folder, self.app._target_code()))
                except Exception:
                    path = None
                self._subtitle_ready.append((key, path))

            threading.Thread(target=work, daemon=True).start()
        if self._subtitle_ready:
            ready_key, ready_path = self._subtitle_ready.pop(0)
            self._subtitle_key, self._subtitle_path = ready_key, ready_path
            self._subtitle_busy = False
            self._subtitle_scaled_key = None
        if key != self._subtitle_key or not self._subtitle_path or not Path(self._subtitle_path).is_file():
            return
        x, y, width, height = subtitle_geometry(values, frame)
        scale = pw / max(1, frame[0])
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        scaled_key = (str(self._subtitle_path), size)
        if scaled_key != self._subtitle_scaled_key:
            try:
                with Image.open(self._subtitle_path) as image:
                    self._subtitle_photo = ImageTk.PhotoImage(image.resize(size, Image.Resampling.LANCZOS))
                self._subtitle_scaled_key = scaled_key
            except Exception:
                return
        self.canvas.create_image(ox + x * scale, oy + y * scale, image=self._subtitle_photo,
                                 anchor="nw", tags=("editor", "subtitle"))

    # --------------------------------------------------------------- seeking
    def _clock(self, seconds: object) -> str:
        text = format_time_seconds(seconds)
        return text or "0:00"

    def _update_time_label(self) -> None:
        self.time_label.configure(text=f"{self._clock(self.time.get())} / {self._clock(self.duration)}")

    def _time_changed(self) -> None:
        self._update_time_label()
        self.redraw()

    def _seek(self, delta: float) -> None:
        value = float(self.time.get()) + delta
        upper = max(0.0, self.duration)
        if upper:
            value = _clamp(value, 0.0, max(0.0, upper - 0.05))
        else:
            value = max(0.0, value)
        self.time.set(value)
        self._update_time_label()
        self.refresh()

    # ------------------------------------------------------------- drag/edit
    def begin_blur_box(self) -> None:
        self.draw_blur = True
        self.canvas.configure(cursor="crosshair")
        self.hint.configure(text=f"Preview {self.letter}: drag a rectangle over the watermark, then release")

    def _press(self, event) -> None:
        x, y = self._xy(event)
        if self.draw_blur:
            self._draw_start = (x, y)
            self._temp_rect = self.canvas.create_rectangle(x, y, x, y, outline="#48d7ff", width=2,
                                                            dash=(6, 4), tags=("editor", "temp_blur"))
            return
        ox, oy, pw, ph = self._bounds()
        fraction = ((x - ox) / max(1, pw), (y - oy) / max(1, ph))
        for item in reversed(self.canvas.find_overlapping(x - 3, y - 3, x + 3, y + 3)):
            tags = self.canvas.gettags(item)
            kind: str | None = None
            index: int | None = None
            original = None
            for tag in tags:
                if tag == "logo":
                    kind, original = "logo", list(self.panel.logo_pos)
                elif tag == "title":
                    kind, original = "title", list(self.panel.title_pos)
                elif tag == "extra_text":
                    kind, original = "extra_text", list(self.panel.extra_pos)
                elif tag == "subtitle":
                    kind, original = "subtitle", list(self.panel.subtitles.position)
                elif tag.startswith("custom_"):
                    index = int(tag.rsplit("_", 1)[1])
                    overlay = self.panel.custom_overlays[index]
                    kind = "custom"
                    original = [float(overlay["x_percent"]), float(overlay["y_percent"])]
                elif tag.startswith("blur_handle_"):
                    index = int(tag.rsplit("_", 1)[1])
                    kind, original = "blur_resize", dict(self.panel.blur_boxes[index])
                elif tag.startswith("blur_"):
                    index = int(tag.rsplit("_", 1)[1])
                    kind, original = "blur_move", dict(self.panel.blur_boxes[index])
                elif tag.startswith("crop_handle_"):
                    index = int(tag.rsplit("_", 1)[1])
                    kind, original = "crop_resize", self.panel._crop_values()
                elif tag == "crop_box":
                    kind, original = "crop_move", self.panel._crop_values()
                if kind:
                    self.panel.selected_blur = index if kind.startswith("blur") else self.panel.selected_blur
                    self.panel.selected_overlay = index if kind == "custom" else self.panel.selected_overlay
                    self._drag = (kind, index, fraction[0], fraction[1], original)
                    self.canvas.configure(cursor="move")
                    return

    def _motion(self, event) -> None:
        x, y = self._xy(event)
        if self._draw_start is not None and self._temp_rect is not None:
            self.canvas.coords(self._temp_rect, self._draw_start[0], self._draw_start[1], x, y)
            return
        if self._drag is None:
            return
        ox, oy, pw, ph = self._bounds()
        kind, index, start_x, start_y, original = self._drag
        dx = (x - ox) / max(1, pw) - start_x
        dy = (y - oy) / max(1, ph) - start_y
        if kind in ("title", "logo", "extra_text", "subtitle"):
            pos = list(original)
            pos[0] = _clamp(pos[0] + dx, 0.0, 1.0)
            pos[1] = _clamp(pos[1] + dy, 0.0, 1.0)
            if kind == "title":
                self.panel.title_pos[:] = pos
                self.panel.sync_position_vars()
            elif kind == "logo":
                self.panel.logo_pos[:] = pos
                self.panel.sync_position_vars()
            elif kind == "extra_text":
                self.panel.extra_pos[:] = pos
                self.panel.sync_position_vars()
            else:
                self.panel.subtitles.position[:] = pos
        elif kind == "custom" and index is not None and 0 <= index < len(self.panel.custom_overlays):
            overlay = self.panel.custom_overlays[index]
            overlay["x_percent"] = _clamp(float(original[0]) + dx, 0.0, 1.0)
            overlay["y_percent"] = _clamp(float(original[1]) + dy, 0.0, 1.0)
        elif kind in ("blur_move", "blur_resize") and index is not None and 0 <= index < len(self.panel.blur_boxes):
            box = dict(original)
            if kind == "blur_move":
                box["x"] = _clamp(box["x"] + dx, 0.0, 1.0 - box["w"])
                box["y"] = _clamp(box["y"] + dy, 0.0, 1.0 - box["h"])
            else:
                left, top = box["x"], box["y"]
                right, bottom = left + box["w"], top + box["h"]
                if dx > 0:
                    right = min(1.0, right + dx)
                else:
                    left = max(0.0, left + dx)
                if dy > 0:
                    bottom = min(1.0, bottom + dy)
                else:
                    top = max(0.0, top + dy)
                box = {"x": left, "y": top, "w": max(0.01, right - left), "h": max(0.01, bottom - top)}
            self.panel.blur_boxes[index] = box
        elif kind in ("crop_move", "crop_resize") and isinstance(original, dict):
            crop = dict(original)
            if kind == "crop_move":
                crop["x_percent"] = _clamp(crop["x_percent"] + dx, 0.0, 1.0 - crop["width_percent"])
                crop["y_percent"] = _clamp(crop["y_percent"] + dy, 0.0, 1.0 - crop["height_percent"])
            else:
                crop["width_percent"] = _clamp(crop["width_percent"] + dx, 0.02, 1.0)
                crop["height_percent"] = _clamp(crop["height_percent"] + dy, 0.02, 1.0)
                lock = ASPECT_LOCKS.get(str(crop.get("aspect_lock") or "free"))
                if lock:
                    fw, fh = self.source_size if self.source_size[0] else (1920, 1080)
                    actual = (crop["width_percent"] * fw) / max(1.0, crop["height_percent"] * fh)
                    if actual > lock:
                        crop["width_percent"] = _clamp(lock * crop["height_percent"] * fh / fw, 0.02, 1.0)
                    else:
                        crop["height_percent"] = _clamp(crop["width_percent"] * fw / (lock * fh), 0.02, 1.0)
            self.panel._apply_crop_values(crop)
        self.panel._changed()
        self.redraw()

    def _release(self, event) -> None:
        if self._draw_start is not None:
            x, y = self._xy(event)
            ox, oy, pw, ph = self._bounds()
            sx, sy = self._draw_start
            left = _clamp(min(sx, x), ox, ox + pw)
            top = _clamp(min(sy, y), oy, oy + ph)
            right = _clamp(max(sx, x), ox, ox + pw)
            bottom = _clamp(max(sy, y), oy, oy + ph)
            if right - left > 6 and bottom - top > 6:
                self.panel.blur_boxes.append({"x": (left - ox) / pw, "y": (top - oy) / ph,
                                              "w": (right - left) / pw, "h": (bottom - top) / ph})
                self.panel.selected_blur = len(self.panel.blur_boxes) - 1
                self.panel._changed()
            self.draw_blur = False
            self.canvas.configure(cursor="crosshair")
            self._draw_start = None
            if self._temp_rect is not None:
                self.canvas.delete(self._temp_rect)
                self._temp_rect = None
            self.hint.configure(text="Ready · drag title, logo, subtitle, crop, blur or image overlay")
        self._drag = None
        self.redraw()

    def _double_click(self, event) -> None:
        x, y = self._xy(event)
        for item in reversed(self.canvas.find_overlapping(x - 3, y - 3, x + 3, y + 3)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("blur_"):
                    index = int(tag.rsplit("_", 1)[1])
                    if 0 <= index < len(self.panel.blur_boxes):
                        self.panel.blur_boxes.pop(index)
                        self.panel.selected_blur = None
                        self.panel._changed()
                        self.redraw()
                    return


class TwoRatioTab(ttk.Frame):
    """Third tool tab: one source, one script, one voice, two independent videos."""

    def __init__(self, master, *, app) -> None:
        super().__init__(master)
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.source_size = (0, 0)
        self._source_job: str | None = None
        self._last_source = ""
        self.previews: dict[str, RatioPreview] = {}
        self._build()
        self.app.source.trace_add("write", lambda *_a: self._source_changed())
        self.after(400, self._probe_source)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        top = ttk.LabelFrame(self, text="Shared source / narration — used by both videos", padding=(8, 6))
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))
        top.columnconfigure(1, weight=3)
        top.columnconfigure(4, weight=2)
        ttk.Label(top, text="Source video").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        source = ttk.Entry(top, textvariable=self.app.source)
        source.grid(row=0, column=1, sticky="ew", pady=3)
        source.bind("<KeyRelease>", lambda _e: self.app._clear_batch())
        ttk.Button(top, text="Browse", style="Compact.TButton",
                   command=self.app._browse_source).grid(row=0, column=2, padx=(6, 4))
        ttk.Button(top, text="Folder", style="Compact.TButton",
                   command=self.app._browse_folder).grid(row=0, column=3)
        ttk.Label(top, text="Gemini .env").grid(row=0, column=4, sticky="w", padx=(10, 6))
        ttk.Entry(top, textvariable=self.app.gemini_env).grid(row=0, column=5, sticky="ew")
        ttk.Button(top, text="Browse", style="Compact.TButton",
                   command=self.app._browse_env).grid(row=0, column=6, padx=(6, 0))

        ttk.Label(top, text="Save final to").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(top, textvariable=self.app.final_folder).grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Button(top, text="Browse", style="Compact.TButton",
                   command=self.app._browse_final_folder).grid(row=1, column=4, sticky="w", padx=(10, 0))
        ttk.Label(top, text="Source language").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Combobox(top, textvariable=self.app.source_language, state="readonly",
                     values=tuple(LANGUAGE_LABELS.values())).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(top, text="Output").grid(row=2, column=2, sticky="w", padx=(10, 6))
        self.target_combo = ttk.Combobox(top, textvariable=self.app.target_language, state="readonly",
                                         values=tuple(TARGET_LABELS.values()), width=26)
        self.target_combo.grid(row=2, column=3, columnspan=2, sticky="w", pady=3)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._target_changed())
        ttk.Label(top, text="Narrator").grid(row=2, column=5, sticky="w", padx=(10, 6))
        self.voice_combo = ttk.Combobox(top, textvariable=self.app.edge_voice, state="readonly",
                                        values=self.app._narrator_voices(), width=28)
        self.voice_combo.grid(row=2, column=6, sticky="ew", pady=3)

        voice = ttk.Frame(top)
        voice.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(6, 0))
        voice.columnconfigure(1, weight=1)
        voice.columnconfigure(4, weight=1)
        ttk.Label(voice, text="Speed", width=8).grid(row=0, column=0, sticky="w")
        ttk.Scale(voice, from_=-50, to=100, orient="horizontal", variable=self.app.edge_rate_percent,
                  command=self._rate_changed).grid(row=0, column=1, sticky="ew")
        self.rate_label = ttk.Label(voice, text=_fmt_rate(self.app.edge_rate_percent.get()), width=7)
        self.rate_label.grid(row=0, column=2, padx=(6, 0))
        ttk.Label(voice, text="Pitch", width=8).grid(row=0, column=3, sticky="w", padx=(10, 0))
        ttk.Scale(voice, from_=-50, to=50, orient="horizontal", variable=self.app.edge_pitch_hz,
                  command=self._pitch_changed).grid(row=0, column=4, sticky="ew")
        self.pitch_label = ttk.Label(voice, text=_fmt_pitch(self.app.edge_pitch_hz.get()), width=8)
        self.pitch_label.grid(row=0, column=5, padx=(6, 0))
        ttk.Label(voice, text="Engine").grid(row=0, column=6, sticky="w", padx=(10, 0))
        engine = ttk.Combobox(voice, textvariable=self.app.tts_engine_label, state="readonly",
                              values=tuple(ENGINE_LABELS.values()), width=20)
        engine.grid(row=0, column=7, sticky="w")
        engine.bind("<<ComboboxSelected>>", lambda _e: self._engine_changed())
        ttk.Button(voice, text="▶ Preview Voice", style="Compact.TButton",
                   command=self.app._preview_voice).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Checkbutton(voice, text="Natural pauses (SSML)", variable=self.app.edge_tts_ssml,
                        command=self._engine_changed).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(voice, text="Voice vol", width=8).grid(row=1, column=3, sticky="w", padx=(10, 0), pady=(4, 0))
        ttk.Spinbox(voice, from_=50, to=300, width=7, textvariable=self.app.voice_volume,
                    command=self._engine_changed).grid(row=1, column=4, sticky="w", pady=(4, 0))

        ttk.Label(top, style="Muted.TLabel", wraplength=980, justify="left",
                  text="One planner run + one narration voice are shared. Each side has its own "
                       "preview and style tools. Video A renders first; Video B reuses the saved "
                       "script and audio and only recomposes the picture. Detailed logs stay on the Recap tab."
                  ).grid(row=4, column=0, columnspan=7, sticky="w", pady=(5, 0))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew", padx=8)

        profiles = self.app.cfg.get("ratio_profiles") or {}
        first_ratio = self.app._ratio_label(self.app.cfg.get("output_ratio") or "16:9")
        second_ratio = self.app._ratio_label(self.app.cfg.get("second_output_ratio") or "9:16")
        if first_ratio == second_ratio:
            second_ratio = "16:9" if first_ratio != "16:9" else "9:16"

        self.panel_a, host_a, canvas_a, side_a = self._make_side(
            panes, "a", "🎬 Video A — first render", first_ratio, profiles)
        self.panel_b, host_b, canvas_b, side_b = self._make_side(
            panes, "b", "🎬 Video B — second render", second_ratio, profiles)
        panes.add(side_a, weight=1)
        panes.add(side_b, weight=1)
        self.app._bind_wheel_recursive(host_a, canvas_a)
        self.app._bind_wheel_recursive(host_b, canvas_b)

        bottom = ttk.Frame(self)
        bottom.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        bottom.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(bottom, text="START 2 VIDEOS", style="Accent.TButton",
                                    command=self._start)
        self.start_btn.grid(row=0, column=0)
        self.app.two_ratio_start_btn = self.start_btn
        self.stop_btn = ttk.Button(bottom, text="STOP SAFELY", style="Danger.TButton",
                                   command=self.app._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(8, 0))
        self.app.two_ratio_stop_btn = self.stop_btn
        ttk.Label(bottom, textvariable=self.app.stage, style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(14, 0))
        ttk.Label(bottom, textvariable=self.app.status, style="Status.TLabel").grid(row=0, column=3)
        ttk.Label(bottom, textvariable=self.app.job_display, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(5, 0))

    def _make_side(self, panes, which: str, name: str, ratio: str, profiles: dict):
        """Create preview + scrollable style tools for one side of the split."""
        side = ttk.PanedWindow(panes, orient="vertical")
        scroll = ttk.Frame(side)
        scroll.rowconfigure(0, weight=1)
        scroll.columnconfigure(0, weight=1)
        canvas = tk.Canvas(scroll, bg="#0d0f14", highlightthickness=0, width=430)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(scroll, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.app._bind_wheel(canvas, canvas)

        panel = StylePanel(inner, app=self.app, name=name, ratio=ratio, letter=which.upper(),
                           on_change=lambda: self._panel_changed(which))
        panel.pack(fill="both", expand=True)
        preview = RatioPreview(side, app=self.app, panel=panel, letter=which.upper())
        panel.preview = preview
        self.previews[which] = preview
        side.add(preview, weight=2)
        side.add(scroll, weight=3)
        panel.load_profile(profiles.get(ratio_key(ratio)) or {})
        return panel, inner, canvas, side

    # ---------------------------------------------------------- shared voice
    def _rate_changed(self, value) -> None:
        self.rate_label.configure(text=_fmt_rate(value))
        if hasattr(self.app, "edge_speed_value"):
            self.app._edge_rate_changed(str(value))

    def _pitch_changed(self, value) -> None:
        self.pitch_label.configure(text=_fmt_pitch(value))
        if hasattr(self.app, "edge_pitch_value"):
            self.app._pitch_changed()

    def _engine_changed(self) -> None:
        if hasattr(self.app, "engine_status_label"):
            self.app._engine_changed()

    def _source_changed(self) -> None:
        if self._source_job is not None:
            self.after_cancel(self._source_job)
        self._source_job = self.after(600, self._probe_source)

    def _probe_source(self) -> None:
        source = self.app.source.get().strip()
        if source != self._last_source and source and Path(source).is_file():
            self.app._clear_batch()
            self._last_source = source
        if not source or not Path(source).is_file():
            self.source_size = (0, 0)
            for preview in self.previews.values():
                preview.source_size = (0, 0)
                preview.duration = 0.0
                preview.refresh()
            return
        try:
            ffprobe = _tool_path(self.app.cfg, "ffprobe")
            self.source_size = probe_video_size(ffprobe, source)
            duration = preview_probe_duration(ffprobe, source)
            for preview in self.previews.values():
                preview.source_size = self.source_size
                preview.duration = duration
                preview.slider.configure(to=max(0.1, duration))
                preview.time.set(_clamp(float(preview.time.get()), 0.0, max(0.0, duration - 0.05)))
                preview.refresh()
        except Exception as exc:                           # noqa: BLE001
            self.app._log(f"⚠️ Two-Ratio preview probe: {exc}")

    # ------------------------------------------------------------------ events
    def _target_changed(self) -> None:
        self.app._target_changed()
        self.voice_combo.configure(values=self.app._narrator_voices())
        families = latin_fonts() if self.app._target_code() == "en" else list(myanmar_fonts())
        for panel in (self.panel_a, self.panel_b):
            try:
                panel.subtitles.set_font_choices(families)
            except Exception:
                pass
        for preview in self.previews.values():
            preview.redraw()

    def _distinct_ratios(self) -> None:
        first = canonical_ratio(self.panel_a.ratio.get())
        if canonical_ratio(self.panel_b.ratio.get()) != first:
            return
        other = "16:9" if first != "16:9" else "9:16"
        self.panel_b.ratio.set(other)
        profiles = self.app.cfg.get("ratio_profiles") or {}
        self.panel_b.load_profile(profiles.get(ratio_key(other)) or default_profile_for_ratio(other))

    def _store_profiles(self) -> None:
        profiles = dict(self.app.cfg.get("ratio_profiles") or {})
        for panel in (self.panel_a, self.panel_b):
            profile = panel.profile()
            profiles[ratio_key(profile["output_ratio"])] = profile
        self.app.cfg["ratio_profiles"] = profiles
        self.app.cfg["output_ratio"] = canonical_ratio(self.panel_a.ratio.get())
        self.app.cfg["second_output_ratio"] = canonical_ratio(self.panel_b.ratio.get())
        self.app.save_cfg()

    def _autosave(self, which: str) -> None:
        panel = self.panel_a if which == "a" else self.panel_b
        if panel._loading:
            return
        self._store_profiles()

    def _panel_changed(self, which: str) -> None:
        self._autosave(which)
        preview = self.previews.get(which)
        if preview is not None:
            preview.schedule_refresh()

    # ------------------------------------------------------------------ start
    def _start(self) -> None:
        if self.app.worker and self.app.worker.is_alive():
            return
        self._distinct_ratios()
        try:
            for panel in (self.panel_a, self.panel_b):
                panel.commit_overlay_times()
            base = self.app._capture_settings()     # validates source, .env, voice and tools once
            first, second, ratio_a, ratio_b = prepare_two_ratio_settings(
                base, self.panel_a.profile(), self.panel_b.profile())
            self._store_profiles()
            self.app.cfg["export_two_ratios"] = True
            self.app._save_cfg(first)               # global settings + Video A profile
            self.app.save_cfg()                     # keep Video B profile too
        except Exception as exc:
            messagebox.showerror("Two-Ratio Setup Error", str(exc), parent=self)
            return
        self.app.start_two_ratio(first, second)
