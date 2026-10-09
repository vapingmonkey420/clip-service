"""Speech to timed words with faster-whisper (runs on CPU, no API key)."""

from __future__ import annotations

import os
import time
import wave

import numpy as np

from .transcript import Transcript, Word, apply_replacements, clean_words
from .util import ClipperError, clock, log


def load_audio(path):
    """Read the mono 16 kHz WAV that ingest wrote, as the samples the model expects.

    Handing the model samples, not a file name, keeps its own audio decoder out
    of the picture; that decoder has broken across library versions before.
    """
    with wave.open(str(path), "rb") as fh:
        if (fh.getnchannels(), fh.getsampwidth(), fh.getframerate()) != (1, 2, 16000):
            raise ClipperError(f"{path} is not 16 kHz mono 16-bit audio.")
        samples = np.frombuffer(fh.readframes(fh.getnframes()), dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def transcribe(audio_path, settings: dict) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ClipperError("Transcription needs the `faster-whisper` package (pip install -r requirements.txt).") from None

    name = settings["model"]
    log.info("Loading speech model %s", name)
    try:
        model = WhisperModel(name, device="auto", compute_type="int8", cpu_threads=os.cpu_count() or 2)
    except Exception as exc:  # download and load errors come from several libraries
        raise ClipperError(
            f"Could not load the speech model '{name}' ({type(exc).__name__}: {str(exc)[:200]}). "
            "The first run downloads it from Hugging Face, so check the network, or pick another "
            "transcribe.model."
        ) from None

    options = dict(
        language=settings.get("language") or None,
        beam_size=int(settings.get("beam_size", 5)),
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        # Each window stands alone, so one bad stretch cannot derail what follows.
        condition_on_previous_text=False,
    )
    vocabulary = [str(term).strip() for term in settings.get("vocabulary") or [] if str(term).strip()]
    if vocabulary:
        options["hotwords"] = ", ".join(vocabulary)

    segments, info = model.transcribe(load_audio(audio_path), **options)
    words: list[Word] = []
    started = last_report = time.monotonic()
    for segment in segments:
        for word in segment.words or []:
            words.append(Word(word.word, word.start, word.end))
        now = time.monotonic()
        if now - last_report >= 60:
            last_report = now
            speed = segment.end / max(now - started, 1e-6)
            log.info("Transcribed %s of %s (%.1fx real time)", clock(segment.end), clock(info.duration), speed)

    words = clean_words(apply_replacements(clean_words(words), settings.get("replace") or {}))
    if len(words) < 20:
        raise ClipperError("Almost no speech was found in this episode, so there is nothing to clip.")
    elapsed = time.monotonic() - started
    log.info("Transcript: %d words in %s (%.1fx real time)", len(words), clock(elapsed), info.duration / max(elapsed, 1e-6))
    return Transcript(words=words, language=info.language or "en", duration=float(info.duration), model=name)
