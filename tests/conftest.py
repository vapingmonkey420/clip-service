"""Shared helpers for the tests. Nothing here needs a network, a speech model, or a GPU."""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipper import config  # noqa: E402
from clipper.transcript import Transcript, Word  # noqa: E402


def make_transcript(lines) -> Transcript:
    """lines: [(text, start_seconds, duration_seconds), ...]; words are spread evenly."""
    words = []
    for text, start, length in lines:
        parts = text.split()
        step = length / len(parts)
        for i, part in enumerate(parts):
            words.append(Word(part, start + i * step, start + (i + 1) * step - 0.02))
    end = max(w.end for w in words)
    return Transcript(words=words, duration=end + 1)


def write_wav(path: Path, spans, total: float, rate: int = 16000, floor: float = 4) -> None:
    """A mono WAV that is a steady tone inside each (start, end[, loudness]) span and near-silent elsewhere."""
    rng = np.random.default_rng(1)
    t = np.arange(int(total * rate)) / rate
    audio = rng.normal(0, floor, t.size)
    for start, end, *level in spans:
        inside = (t >= start) & (t < end)
        audio[inside] += (level[0] if level else 6000) * np.sin(2 * np.pi * 220 * t[inside])
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(np.clip(audio, -32768, 32767).astype(np.int16).tobytes())


@pytest.fixture
def cfg() -> dict:
    """The built-in defaults, as a client would get them."""
    settings = config.merge(config.DEFAULTS, {"name": "Test Show"})
    settings["slug"] = "test"
    return settings


@pytest.fixture
def talk() -> Transcript:
    """Two minutes of made-up conversation with clear pauses between topics."""
    return make_transcript(
        [
            ("Welcome back to the show everyone.", 0.0, 2.5),
            ("Before we start thanks for the reviews.", 2.8, 3.0),
            ("Here is the thing nobody tells you about pricing.", 8.0, 4.0),
            ("Most founders charge a third of what they should.", 12.2, 4.0),
            ("They are scared the customer will walk away.", 16.4, 3.5),
            ("So they discount before anyone even asks.", 20.1, 3.5),
            ("Raise the price and watch who stays.", 23.8, 3.0),
            ("Okay let me check my notes.", 30.0, 2.0),
            ("What is the biggest mistake you made in year one?", 35.0, 4.0),
            ("I hired too fast and I hired my friends.", 39.3, 4.0),
            ("It took me a year to undo that.", 43.5, 3.0),
            ("Never hire someone you cannot fire.", 46.8, 3.0),
            ("That is all the time we have.", 55.0, 2.5),
            ("See you next week.", 57.8, 1.5),
        ]
    )
