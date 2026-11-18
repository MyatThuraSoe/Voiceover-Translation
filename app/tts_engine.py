"""TTS engine abstraction (V2, 2026-11): one interface over Edge TTS, Meta MMS
(facebook/mms-tts-mya, local + free), Piper, ElevenLabs free tier and Gemini TTS.

Design (spec §7):
- Every engine implements TTSEngineBase: generate / get_voices / is_available / preview.
- The Smart Sync pipeline in core.py speaks "generate_channel99_voice(text, voice) → WAV path";
  adapters provide that method, so the render loop never changes shape.
- TTSFallbackChain tries the selected engine 3 times, then the next available
  engine in TTS_FALLBACK_ORDER, logging "⚠️ X failed, falling back to Y".

Heavy dependencies (torch / transformers / scipy) are imported lazily: when they
are not installed the engine simply reports is_available() == False.
"""
from __future__ import annotations

import os
import subprocess
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from app_paths import data_root

LogFn = Callable[[str], None]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TTS_FALLBACK_ORDER = [
    "mms_myanmar",   # best quality, local (needs transformers + torch)
    "edge_tts",      # reliable, free (default when MMS not installed)
    "gemini_tts",    # needs GEMINI_API_KEY in .env
    "elevenlabs",    # free tier 10k chars/month
]

ENGINE_LABELS = {
    "edge_tts": "Edge TTS (Free)",
    "mms_myanmar": "MMS Myanmar (Local)",
    "piper": "Piper (Local)",
    "gemini_tts": "Gemini TTS (.env key)",
    "elevenlabs": "ElevenLabs (Free tier)",
}

PREVIEW_TEXT = "မင်္ဂလာပါ။ ဒါသည် အသံ နမူနာ ဖြစ်ပါတယ်။"
MMS_MODEL_ID = "facebook/mms-tts-mya"


class TTSEngineBase(ABC):
    """Base class for all TTS engines (spec §7.3)."""

    name: str = "base"

    @abstractmethod
    def generate(self, text: str, voice: str, output_path: Path, **kwargs) -> Path:
        """Generate audio file (WAV), return its path."""

    @abstractmethod
    def get_voices(self) -> list[dict]:
        """Return list of available voices: [{"id": ..., "label": ...}]."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if engine is ready to use (deps installed, key present, model present)."""

    def preview(self, text: str, voice: str, output_path: Path) -> Path:
        """Generate a short preview clip."""
        return self.generate(text or PREVIEW_TEXT, voice, output_path)

    # Smart Sync adapter interface -------------------------------------------
    def generate_channel99_voice(self, text: str, voice: str) -> str:
        """Pipeline-compatible entry: WAV at 24 kHz mono (like core.EdgeTTSEngine)."""
        out = Path(self._work_dir()) / f"{self.name}_{abs(hash((voice, text))) & 0xFFFFFFFF:x}.wav"
        if out.is_file() and out.stat().st_size > 1000:
            return str(out)
        return str(self.generate(text, voice, out))


class _WavWriterMixin:
    def _work_dir(self) -> Path:
        path = Path(getattr(self, "work_dir", Path(".tts_cache")))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _write_wav(path: Path, samples: Any, rate: int) -> Path:
        import numpy as np
        data = np.asarray(samples, dtype="<f4").squeeze()
        if data.ndim == 0:
            data = data.reshape(1)
        pcm = np.clip(data, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2")
        if pcm16.ndim > 1:
            pcm16 = pcm16[:, 0]
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes(pcm16.tobytes())
        return path

    def _convert_wav(self, ffmpeg: str, src: Path, dst: Path, rate: int = 24000) -> Path:
        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
        if result.returncode != 0 or not dst.is_file():
            raise RuntimeError(f"WAV conversion failed: {(result.stderr or '')[-600:]}")
        return dst


class EdgeTTSEngineAdapter(TTSEngineBase, _WavWriterMixin):
    """Wraps the existing core.EdgeTTSEngine (same retry/proxy logic)."""

    name = "edge_tts"

    def __init__(self, engine) -> None:  # engine: core.EdgeTTSEngine
        self.engine = engine
        self.work_dir = engine.work_dir

    def generate(self, text: str, voice: str, output_path: Path, **kwargs) -> Path:
        produced = Path(self.engine.generate_channel99_voice(text, voice))
        if produced.resolve() == Path(output_path).resolve():
            return produced
        import shutil
        shutil.copy2(produced, output_path)
        return Path(output_path)

    def get_voices(self) -> list[dict]:
        return [
            {"id": "my-MM-ThihaNeural", "label": "Myanmar - Thiha (Male)"},
            {"id": "my-MM-NilarNeural", "label": "Myanmar - Nilar (Female)"},
        ]

    def is_available(self) -> bool:
        return bool(self.engine)

    def generate_channel99_voice(self, text: str, voice: str) -> str:
        return self.engine.generate_channel99_voice(text, voice)


class MMSMyanmarEngine(TTSEngineBase, _WavWriterMixin):
    """Meta MMS TTS for Myanmar (facebook/mms-tts-mya) — local and free.

    The model (~350 MB) is downloaded on first use and cached in <app>/models/mms-mya.
    Voice is fixed (one voice per language model), so get_voices() returns one entry.
    """

    name = "mms_myanmar"

    def __init__(self, model_dir: str | Path | None = None, log: LogFn = print) -> None:
        root = data_root()
        self.model_dir = Path(model_dir) if model_dir else root / "models" / "mms-mya"
        self.model = None
        self.tokenizer = None
        self.log = log

    def is_available(self) -> bool:
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_model(self) -> None:
        if self.model is not None:
            return
        if not self.is_available():
            raise RuntimeError(
                "MMS Myanmar TTS needs transformers + torch + scipy. Install with:\n"
                "  python -m pip install transformers torch scipy"
            )
        from transformers import VitsModel, AutoTokenizer
        if self.model_dir.is_dir() and any(self.model_dir.glob("*.json")):
            self.log("⏳ MMS Myanmar: loading local model...")
            self.model = VitsModel.from_pretrained(str(self.model_dir))
            self.tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        else:
            self.log("⏳ MMS Myanmar: downloading model (first use only, ~350 MB)...")
            self.model = VitsModel.from_pretrained(MMS_MODEL_ID)
            self.tokenizer = AutoTokenizer.from_pretrained(MMS_MODEL_ID)
            try:
                self.model.save_pretrained(str(self.model_dir))
                self.tokenizer.save_pretrained(str(self.model_dir))
            except Exception as exc:
                self.log(f"⚠️ MMS model cache save failed ({exc}) — it will re-download next time")
        self.model.eval()

    def generate(self, text: str, voice: str, output_path: Path, **kwargs) -> Path:
        self._ensure_model()
        import torch
        clean = " ".join(str(text or "").split()).strip()
        if not clean:
            raise ValueError("MMS TTS text is empty")
        inputs = self.tokenizer(clean, return_tensors="pt")
        with torch.no_grad():
            output = self.model(**inputs).waveform
        rate = self.model.config.sampling_rate
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self._write_wav(output_path, output.numpy(), rate)

    def get_voices(self) -> list[dict]:
        return [{"id": MMS_MODEL_ID, "label": "MMS Myanmar (single local voice)"}]


def download_mms_model(target_dir: Path, log_fn: LogFn | None = None) -> bool:
    """First-time setup: download facebook/mms-tts-mya into target_dir (spec §7.6)."""
    log = log_fn or print
    try:
        from transformers import VitsModel, AutoTokenizer
        target_dir = Path(target_dir)
        log("Downloading MMS Myanmar TTS model (~350MB)...")
        model = VitsModel.from_pretrained(MMS_MODEL_ID)
        tokenizer = AutoTokenizer.from_pretrained(MMS_MODEL_ID)
        target_dir.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(str(target_dir))
        tokenizer.save_pretrained(str(target_dir))
        log("✅ MMS Myanmar model ready")
        return True
    except Exception as exc:
        log(f"❌ MMS download failed: {exc}")
        return False


class PiperTTSEngine(TTSEngineBase, _WavWriterMixin):
    """Piper TTS — fast local inference via the piper-tts Python package.

    Burmese models are rare; the engine runs any .onnx voice model the user
    points it to via PIPER_MODEL (path or HuggingVoice name)."""

    name = "piper"

    def __init__(self, model_ref: str | None = None, ffmpeg: str = "ffmpeg", log: LogFn = print) -> None:
        self.model_ref = model_ref or os.getenv("PIPER_MODEL", "")
        self.ffmpeg = ffmpeg
        self.work_dir = Path(".tts_cache") / "piper"
        self.log = log

    def is_available(self) -> bool:
        try:
            import piper  # noqa: F401
            return True
        except ImportError:
            return False

    def generate(self, text: str, voice: str, output_path: Path, **kwargs) -> Path:
        from piper import PiperVoice
        model_path = self.model_ref or voice
        if not model_path:
            raise RuntimeError("Piper: no voice model (set PIPER_MODEL or choose a .onnx voice)")
        piper_voice = PiperVoice.load(model_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        import numpy as np
        chunks: list[np.ndarray] = []
        rate = int(getattr(piper_voice.config, "sample_rate", 22050))
        for chunk in piper_voice.synthesize(str(text or "").strip(), sentence_silence=0.25):
            chunks.append(np.asarray(chunk.audio, dtype="<f2").astype(np.float32) / 32768.0)
        if not chunks:
            raise RuntimeError("Piper produced no audio")
        return self._write_wav(output_path, np.concatenate(chunks), rate)

    def get_voices(self) -> list[dict]:
        ref = self.model_ref or "PIPER_MODEL"
        return [{"id": str(ref), "label": f"Piper model: {Path(str(ref)).name}"}]


class ElevenLabsEngine(TTSEngineBase, _WavWriterMixin):
    """ElevenLabs API with free-tier support (10k chars/month).

    Key: ELEVENLABS_API_KEY in .env or environment. Voice IDs can be passed as
    the `voice` argument; defaults to a multilingual voice when unset."""

    name = "elevenlabs"
    API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel — available on the free tier
    PREVIEW_CHARS_KEY = "elevenlabs_chars_used_month"

    def __init__(self, api_key: str = "", env_file: str = "", ffmpeg: str = "ffmpeg",
                 log: LogFn = print) -> None:
        self.api_key = api_key or self._read_key(env_file)
        self.ffmpeg = ffmpeg
        self.work_dir = Path(".tts_cache") / "elevenlabs"
        self.log = log
        self.chars_this_month = 0

    @staticmethod
    def _read_key(env_file: str) -> str:
        if env_file and Path(env_file).is_file():
            from dotenv import dotenv_values
            value = dotenv_values(env_file).get("ELEVENLABS_API_KEY")
            if value:
                return str(value).strip()
        return str(os.getenv("ELEVENLABS_API_KEY") or "").strip()

    def is_available(self) -> bool:
        return bool(self.api_key)

    def generate(self, text: str, voice: str, output_path: Path, **kwargs) -> Path:
        if not self.api_key:
            raise RuntimeError("ElevenLabs: ELEVENLABS_API_KEY not found in .env / environment")
        import requests
        requested = str(voice or "").strip()
        voice_id = requested if requested and not requested.startswith(
            ("elevenlabs:", "my-MM", "gemini:")) else self.DEFAULT_VOICE_ID
        clean = " ".join(str(text or "").split()).strip()
        if not clean:
            raise ValueError("ElevenLabs text is empty")
        response = requests.post(
            self.API_URL.format(voice_id=voice_id),
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
            json={"text": clean, "model_id": "eleven_multilingual_v2"},
            timeout=120,
        )
        if response.status_code == 401:
            raise RuntimeError("ElevenLabs: API key rejected (401). Check ELEVENLABS_API_KEY.")
        if response.status_code == 429:
            raise RuntimeError("ElevenLabs: free-tier character quota used up (429). Try next month.")
        response.raise_for_status()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw = output_path.with_suffix(".mp3")
        raw.write_bytes(response.content)
        self.chars_this_month += len(clean)
        return self._convert_wav(self.ffmpeg, raw, output_path)

    def get_voices(self) -> list[dict]:
        return [{"id": self.DEFAULT_VOICE_ID, "label": "ElevenLabs — Rachel (free tier)"}]


class TTSFallbackChain:
    """Engine selector with the spec §7.7 fallback behaviour.

    Tries the primary engine up to max_tries times; on persistent failure moves
    to the next AVAILABLE engine in TTS_FALLBACK_ORDER and logs the switch.
    """

    def __init__(self, engines: list[TTSEngineBase], primary: str, log: LogFn = print,
                 max_tries: int = 3) -> None:
        self.engines = {e.name: e for e in engines}
        self.log = log
        self.max_tries = max(1, int(max_tries))
        if primary not in self.engines:
            fallback = engines[0].name if engines else "edge_tts"
            if engines:
                self.log(f"⚠️ TTS engine '{primary}' is not available on this machine — using {fallback}")
            primary = fallback
        self.primary = primary
        self.active = self.primary
        self.fallbacks_used: list[str] = []

    def _order(self) -> list[str]:
        names = list(self.engines)
        ordered = [self.active] if self.active in names else []
        for name in TTS_FALLBACK_ORDER:
            if name in self.engines and name not in ordered:
                ordered.append(name)
        for name in names:  # any engine not in the documented order (e.g. piper)
            if name not in ordered:
                ordered.append(name)
        return ordered

    def generate_channel99_voice(self, text: str, voice: str) -> str:
        errors: list[str] = []
        for name in self._order():
            engine = self.engines[name]
            if not engine.is_available():
                continue
            for attempt in range(1, self.max_tries + 1):
                try:
                    return engine.generate_channel99_voice(text, voice)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}#{attempt}: {type(exc).__name__}: {str(exc)[:160]}")
                    self.log(f"⚠️ TTS {name} attempt {attempt}/{self.max_tries} failed: {str(exc)[:160]}")
                    if attempt < self.max_tries:
                        time.sleep(min(2.0 * attempt, 5.0))
            if name != self.active:
                continue
            remaining = [n for n in self._order()[1:] if self.engines[n].is_available()]
            if remaining:
                self.log(f"⚠️ {name} failed, falling back to {remaining[0]}")
                self.fallbacks_used.append(remaining[0])
                self.active = remaining[0]
        raise RuntimeError(
            "All TTS engines failed: " + " | ".join(errors[-6:])
        )

    def status_lines(self) -> list[str]:
        lines = []
        for name in TTS_FALLBACK_ORDER:
            engine = self.engines.get(name)
            if engine is None:
                continue
            if engine.is_available():
                detail = "Ready"
                if name == "elevenlabs" and isinstance(engine, ElevenLabsEngine):
                    remaining = max(0, 10_000 - engine.chars_this_month)
                    detail = f"{remaining:,} chars remaining this month (approx)"
                if name == "mms_myanmar":
                    detail = "Model loadable" + (" (cached)" if engine.model_dir.is_dir() and
                                                 any(engine.model_dir.glob('*.json')) else " (downloads on first use)")
                lines.append(f"✅ {ENGINE_LABELS.get(name, name)}: {detail}")
            else:
                lines.append(f"⚪ {ENGINE_LABELS.get(name, name)}: not installed / not configured")
        return lines


def create_tts_engines(*, ffmpeg: str, work_dir: Path, rate: str, pitch: str,
                       env_file: str = "", direction: str = "", log: LogFn = print,
                       edge_engine: Any | None = None) -> dict[str, TTSEngineBase]:
    """Every engine the current machine can offer, keyed by engine name.

    core.EdgeTTSEngine (with its resilient proxy retry routes) is injected as
    edge_engine by core.EdgeSmartSync — this module never imports core itself.
    """
    engines: dict[str, TTSEngineBase] = {}
    if edge_engine is not None:
        engines["edge_tts"] = EdgeTTSEngineAdapter(edge_engine)
    try:
        mms = MMSMyanmarEngine(log=log)
        if mms.is_available():
            engines["mms_myanmar"] = mms
    except Exception as exc:
        log(f"⚠️ MMS Myanmar engine unavailable: {exc}")
    try:
        piper = PiperTTSEngine(ffmpeg=ffmpeg, log=log)
        if piper.is_available():
            engines["piper"] = piper
    except Exception as exc:
        log(f"⚠️ Piper engine unavailable: {exc}")
    try:
        eleven = ElevenLabsEngine(env_file=env_file, ffmpeg=ffmpeg, log=log)
        if eleven.is_available():
            engines["elevenlabs"] = eleven
    except Exception as exc:
        log(f"⚠️ ElevenLabs engine unavailable: {exc}")
    return engines
