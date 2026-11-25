"""Dedicated Two-Ratio tab: generate two videos in one job.

Video A and Video B each own a full style profile (ratio, fill, text sizes,
positions, subtitles, logo, crop, colour and ducking). The shared source /
language / voice settings are the same Tk variables used by the Recap tab, so
there is still only one script and one narration voice for both outputs.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import OUTPUT_RATIOS, QUALITY_LABELS
from recap_planner import SOURCE_LANGUAGES
from recap_subtitles import SubtitleControls
from render_variants import canonical_ratio, default_profile_for_ratio, prepare_two_ratio_settings, profile_from_settings, ratio_key
from subtitle_fonts import latin_fonts, myanmar_fonts

LANGUAGE_LABELS = {"auto": "Auto-detect", **SOURCE_LANGUAGES}
TARGET_LABELS = {"mm": "Myanmar  ·  translate", "en": "English  ·  rewrite"}
FILL_LABELS = {"blur": "Blur background", "black": "Black bars", "crop": "Crop to fill"}


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


class StylePanel(ttk.Frame):
    """All style controls for one of the two output videos."""

    def __init__(self, master, *, app, name: str, ratio: str, on_change) -> None:
        super().__init__(master, padding=(8, 6))
        self.app = app
        self.name = name
        self.on_change = on_change
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

        self.crop_enabled = tk.BooleanVar(value=False)
        self.crop_x = tk.DoubleVar(value=0.0)
        self.crop_y = tk.DoubleVar(value=0.0)
        self.crop_w = tk.DoubleVar(value=1.0)
        self.crop_h = tk.DoubleVar(value=1.0)
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
        ttk.Separator(self).grid(row=1, column=0, sticky="ew", pady=(4, 8))

        row = 2
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
                        command=self._changed).grid(row=0, column=0, columnspan=2, sticky="w")
        for r, (text, var, low, high) in enumerate((
                ("Crop X", self.crop_x, 0.0, 1.0), ("Crop Y", self.crop_y, 0.0, 1.0),
                ("Crop W", self.crop_w, 0.02, 1.0), ("Crop H", self.crop_h, 0.02, 1.0)), start=1):
            self._slider(advanced, r, text, low, high, var, "{:.2f}")
        ttk.Separator(advanced).grid(row=5, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Checkbutton(advanced, text="Enable colour correction", variable=self.color_enabled,
                        command=self._changed).grid(row=6, column=0, columnspan=2, sticky="w")
        self._slider(advanced, 7, "Brightness", -0.5, 0.5, self.color_brightness, "{:+.2f}")
        self._slider(advanced, 8, "Contrast", 0.5, 2.0, self.color_contrast, "{:.2f}")
        self._slider(advanced, 9, "Saturation", 0.0, 3.0, self.color_saturation, "{:.2f}")
        ttk.Separator(advanced).grid(row=10, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Checkbutton(advanced, text="Duck background audio", variable=self.duck_enabled,
                        command=self._changed).grid(row=11, column=0, columnspan=2, sticky="w")
        self._number(advanced, 12, "Duck %", self.duck_level, 10, 80)

        for var in (self.ratio, self.fill, self.quality, self.title_text, self.title_size,
                    self.extra_text, self.extra_size, self.extra_opacity, self.logo_path,
                    self.logo_size, self.logo_opacity):
            var.trace_add("write", lambda *_a: self._changed())
        self._format_labels()

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
            pos[index] = max(0.0, min(1.0, float(vars_[index].get())))
        except (ValueError, tk.TclError):
            vars_[index].set(f"{pos[index]:.2f}")
            return
        vars_[index].set(f"{pos[index]:.2f}")
        self._changed()

    def _format_labels(self) -> None:
        for variable, label, fmt in self._labels:
            try:
                label.configure(text=fmt.format(float(variable.get())))
            except (ValueError, tk.TclError):
                pass
        if self.winfo_exists():
            self.after(300, self._format_labels)

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
            self.logo_size.set(_as_float(values.get("logo_width_frac"), 0.16))
            self.logo_opacity.set(_as_float(values.get("logo_opacity"), 0.65))

            for attr, key, fallback in (
                    (self.title_pos, "title_position", [0.5, 0.12]),
                    (self.logo_pos, "logo_position", [0.8, 0.06]),
                    (self.extra_pos, "extra_text_position", [0.5, 0.84])):
                raw = list(values.get(key) or fallback)[:2]
                attr[:] = [max(0.0, min(1.0, _as_float(v, fallback[i]))) for i, v in enumerate(raw)]
            for pos, vars_ in ((self.title_pos, self.title_pos_vars), (self.logo_pos, self.logo_pos_vars),
                               (self.extra_pos, self.extra_pos_vars)):
                for index in (0, 1):
                    vars_[index].set(f"{pos[index]:.2f}")

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
            self.color_enabled.set(bool(color.get("enabled", False)))
            self.color_brightness.set(_as_float(color.get("brightness"), 0.0))
            self.color_contrast.set(_as_float(color.get("contrast"), 1.0))
            self.color_saturation.set(_as_float(color.get("saturation"), 1.0))
            self.duck_enabled.set(bool(duck.get("enabled", False)))
            self.duck_level.set(_as_int(duck.get("duck_volume_percent"), 30))
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
            "frame_fill": next((code for code, label in FILL_LABELS.items() if label == self.fill.get()), "blur"),
            "video_quality": next((code for code, label in QUALITY_LABELS.items() if label == self.quality.get()), "1080"),
            "zoom_factor": float(self.zoom.get()),
            "mirror_video": bool(self.mirror.get()),
            "overlays": overlays,
            "crop": {
                "enabled": bool(self.crop_enabled.get()),
                "x_percent": float(self.crop_x.get()), "y_percent": float(self.crop_y.get()),
                "width_percent": float(self.crop_w.get()), "height_percent": float(self.crop_h.get()),
            },
            "color_correction": {
                "enabled": bool(self.color_enabled.get()),
                "brightness": float(self.color_brightness.get()),
                "contrast": float(self.color_contrast.get()),
                "saturation": float(self.color_saturation.get()),
            },
            "audio_ducking": {
                "enabled": bool(self.duck_enabled.get()),
                "duck_volume_percent": int(self.duck_level.get()),
            },
        }
        return profile_from_settings(settings)


class TwoRatioTab(ttk.Frame):
    """Third tool tab: one source, one script, one voice, two independent videos."""

    def __init__(self, master, *, app) -> None:
        super().__init__(master)
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build()

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
        ttk.Label(top, style="Muted.TLabel", wraplength=900, justify="left",
                  text="One planner run + one narration voice are shared. Video A renders first; "
                       "Video B reuses the saved script and audio and only recomposes the picture. "
                       "Detailed logs stay on the Recap tab."
                  ).grid(row=3, column=0, columnspan=7, sticky="w", pady=(5, 0))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew", padx=8)
        host_a, inner_a, canvas_a = self._scrollable(panes)
        host_b, inner_b, canvas_b = self._scrollable(panes)
        panes.add(host_a, weight=1)
        panes.add(host_b, weight=1)

        profiles = self.app.cfg.get("ratio_profiles") or {}
        first_ratio = self.app._ratio_label(self.app.cfg.get("output_ratio") or "16:9")
        second_ratio = self.app._ratio_label(self.app.cfg.get("second_output_ratio") or "9:16")
        if first_ratio == second_ratio:
            second_ratio = "16:9" if first_ratio != "16:9" else "9:16"
        self.panel_a = StylePanel(inner_a, app=self.app, name="🎬 Video A — first render",
                                  ratio=first_ratio, on_change=lambda: self._autosave("a"))
        self.panel_b = StylePanel(inner_b, app=self.app, name="🎬 Video B — second render",
                                  ratio=second_ratio, on_change=lambda: self._autosave("b"))
        self.panel_a.pack(fill="both", expand=True)
        self.panel_b.pack(fill="both", expand=True)
        self.app._bind_wheel_recursive(inner_a, canvas_a)
        self.app._bind_wheel_recursive(inner_b, canvas_b)
        self.panel_a.load_profile(profiles.get(ratio_key(first_ratio)) or {})
        self.panel_b.load_profile(profiles.get(ratio_key(second_ratio)) or {})
        self._distinct_ratios()

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

    def _scrollable(self, master) -> tuple[ttk.Frame, ttk.Frame, tk.Canvas]:
        host = ttk.Frame(master)
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)
        canvas = tk.Canvas(host, bg="#0d0f14", highlightthickness=0, width=430)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.app._bind_wheel(canvas, canvas)
        return host, inner, canvas

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

    # ------------------------------------------------------------------ start
    def _start(self) -> None:
        if self.app.worker and self.app.worker.is_alive():
            return
        self._distinct_ratios()
        try:
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
