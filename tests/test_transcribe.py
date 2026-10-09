"""The speech step, tested against a stand-in model so no download is needed."""

import inspect
import sys
import types
from collections import namedtuple

import numpy as np
import pytest

from clipper import transcribe as stt
from clipper.util import ClipperError

from conftest import write_wav

FakeWord = namedtuple("FakeWord", "start end word probability")
FakeSegment = namedtuple("FakeSegment", "start end words")
FakeInfo = namedtuple("FakeInfo", "language duration")


def install_fake_model(monkeypatch, segments, calls):
    class WhisperModel:
        def __init__(self, name, **kwargs):
            calls["init"] = (name, kwargs)

        def transcribe(self, audio, **options):
            calls["transcribe"] = (audio, options)
            return iter(segments), FakeInfo("en", 120.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))


def spoken(text: str, start: float = 0.0):
    words = [FakeWord(start + i * 0.4, start + i * 0.4 + 0.3, f" {w}", 0.9) for i, w in enumerate(text.split())]
    return FakeSegment(words[0].start, words[-1].end, words)


SETTINGS = {"model": "small.en", "language": "en", "beam_size": 5, "vocabulary": [], "replace": {}}


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.wav"
    write_wav(path, [(0.5, 1.5)], total=2.0)
    return path


def test_words_and_timings_come_through(monkeypatch, audio):
    calls = {}
    text = "Here is the thing nobody tells you about pricing. Most founders charge about a third of what they really should charge."
    install_fake_model(monkeypatch, [spoken(text)], calls)
    transcript = stt.transcribe(audio, SETTINGS)
    assert [w.text for w in transcript.words[:3]] == ["Here", "is", "the"]  # leading spaces trimmed
    assert transcript.words[1].start == pytest.approx(0.4) and transcript.duration == 120.0
    assert len(transcript.sentences) == 2 and transcript.model == "small.en"
    name, kwargs = calls["init"]
    assert name == "small.en" and kwargs["compute_type"] == "int8"
    samples, options = calls["transcribe"]
    # The model is handed samples (16 kHz mono floats in -1..1), never a file name.
    assert isinstance(samples, np.ndarray) and samples.dtype == np.float32 and samples.size == 32000
    assert 0.1 < float(np.abs(samples).max()) <= 1.0
    assert options["word_timestamps"] is True and options["vad_filter"] is True and options["language"] == "en"
    assert "hotwords" not in options


def test_vocabulary_and_fixups_are_applied(monkeypatch, audio):
    calls = {}
    text = "We moved everything over to crowd strike last year and it took about six months to finish the whole migration project."
    install_fake_model(monkeypatch, [spoken(text)], calls)
    settings = {**SETTINGS, "vocabulary": ["CrowdStrike", "Sentinel"], "replace": {"crowd strike": "CrowdStrike"}}
    transcript = stt.transcribe(audio, settings)
    assert calls["transcribe"][1]["hotwords"] == "CrowdStrike, Sentinel"
    assert "CrowdStrike" in [w.text for w in transcript.words]
    assert "crowd" not in [w.text for w in transcript.words]


def test_near_silent_episode_is_reported_plainly(monkeypatch, audio):
    install_fake_model(monkeypatch, [spoken("Just a few words here.")], {})
    with pytest.raises(ClipperError, match="Almost no speech"):
        stt.transcribe(audio, SETTINGS)


def test_audio_in_the_wrong_shape_is_refused(tmp_path):
    path = tmp_path / "audio.wav"
    write_wav(path, [(0.0, 1.0)], total=1.0, rate=44100)
    with pytest.raises(ClipperError, match="not 16 kHz mono"):
        stt.load_audio(path)


def test_model_that_will_not_load_is_reported_plainly(monkeypatch):
    class WhisperModel:
        def __init__(self, *args, **kwargs):
            raise OSError("could not reach huggingface.co")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    with pytest.raises(ClipperError, match="Could not load the speech model 'small.en'"):
        stt.transcribe("audio.wav", SETTINGS)


def test_the_real_library_still_accepts_what_we_pass():
    """Guards against the installed faster-whisper renaming an option we rely on."""
    faster_whisper = pytest.importorskip("faster_whisper")
    accepted = inspect.signature(faster_whisper.WhisperModel.transcribe).parameters
    for option in ("language", "beam_size", "word_timestamps", "vad_filter", "vad_parameters",
                   "condition_on_previous_text", "hotwords"):  # fmt: skip
        assert option in accepted, option
    created = inspect.signature(faster_whisper.WhisperModel.__init__).parameters
    for option in ("device", "compute_type", "cpu_threads"):
        assert option in created, option
    fields = faster_whisper.transcribe.Word._fields if hasattr(faster_whisper.transcribe.Word, "_fields") else faster_whisper.transcribe.Word.__dataclass_fields__
    assert {"start", "end", "word"} <= set(fields)
    assert "small.en" in faster_whisper.available_models() and "tiny.en" in faster_whisper.available_models()
