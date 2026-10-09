"""Find real pauses in the audio, then use them to cut cleanly and trim dead air.

Speech-model word timings can be a few tenths of a second off, which is enough
to clip a word. The loudness of the audio itself says where speech actually
stops and starts, so every cut is placed inside a measured pause.
"""

from __future__ import annotations

import wave

import numpy as np

from .transcript import Word

FRAME = 0.02  # seconds per loudness sample
LEAD_IN = 0.10  # quiet kept before the first word of a clip
TAIL = 0.30  # quiet kept after the last word of a clip
KEEP_AFTER = 0.18  # of a shortened pause, what stays after the earlier word
KEEP_BEFORE = 0.12  # and what stays before the next word


def find_silences(wav_path, min_len: float = 0.25) -> list[tuple[float, float]]:
    """Return (start, end) spans where the audio is quiet, from a mono 16-bit WAV."""
    levels = _levels(wav_path)
    if levels.size == 0:
        return []
    loud, quiet = np.percentile(levels, 95), np.percentile(levels, 10)
    if loud - quiet < 18:
        # A music bed or heavy noise: no dependable pauses, so report none.
        return []
    threshold = max(loud - 30, quiet + 6)
    silent = levels < threshold
    edges = np.flatnonzero(np.diff(np.concatenate([[False], silent, [False]]).astype(np.int8)))
    spans = []
    for begin, end in zip(edges[::2], edges[1::2]):
        if (end - begin) * FRAME >= min_len:
            spans.append((float(begin * FRAME), float(end * FRAME)))
    return spans


def _levels(wav_path) -> np.ndarray:
    """Loudness in dBFS for each 20 ms of audio."""
    with wave.open(str(wav_path), "rb") as fh:
        if fh.getsampwidth() != 2:
            raise ValueError("expected 16-bit audio")
        channels, rate = fh.getnchannels(), fh.getframerate()
        step = int(rate * FRAME)
        out = []
        while True:
            raw = fh.readframes(step * 5000)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            usable = samples.size - samples.size % step
            if usable == 0:
                continue
            power = (samples[:usable].reshape(-1, step) ** 2).mean(axis=1)
            out.append(10 * np.log10(power / 32768.0**2 + 1e-10))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def pauses_after_words(words: list[Word], silences) -> dict[int, float]:
    """Map each measured pause to the word it follows: {word index: pause length}.

    A pause belongs after the last word that had already started when the quiet
    began. Going by starts keeps this right even when the transcript stretches
    the next word back across the pause.
    """
    out: dict[int, float] = {}
    index = -1
    for a, b in silences:
        while index + 1 < len(words) and words[index + 1].start < a + 0.1:
            index += 1
        if 0 <= index < len(words) - 1:
            out[index] = max(out.get(index, 0.0), b - a)
    return out


def snap(words: list[Word], first: int, last: int, silences, duration: float) -> tuple[float, float]:
    """Pick clip start and end times for words[first..last] that do not clip speech."""
    head, tail = words[first], words[last]
    previous_end = words[first - 1].end if first > 0 else 0.0
    next_start = words[last + 1].start if last + 1 < len(words) else duration

    # A pause that ends about where the first word begins marks where speech really starts.
    onsets = [(a, b) for a, b in silences if head.start - 0.45 <= b <= head.start + 0.20]
    if onsets:
        a, b = min(onsets, key=lambda span: abs(span[1] - head.start))
        start = max(a + 0.02, b - LEAD_IN)
    else:
        gap = head.start - previous_end
        start = head.start - min(LEAD_IN, max(gap, 0.0) / 2)
    # Never open on dead air. Transcripts sometimes stretch a word back across the pause
    # before it; if the clip would still begin inside a pause, begin where that pause ends.
    for a, b in silences:
        if a <= start + 0.05 and start + LEAD_IN + 0.05 < b < tail.start:
            start = b - LEAD_IN

    # The first pause that begins once the last word is under way marks its true end.
    offsets = [(a, b) for a, b in silences if tail.start + 0.05 <= a <= tail.end + 0.45 and a <= next_start + 0.2]
    if offsets:
        a, b = offsets[0]
        end = min(a + TAIL, b - 0.02)
    else:
        gap = next_start - tail.end
        end = tail.end + min(0.15, max(gap, 0.0) / 2)
    # Nor close on it: a clip that would end deep inside a pause ends shortly after the pause begins.
    for a, b in silences:
        if head.end < a < end - TAIL - 0.05 and b >= end - 0.05:
            end = a + TAIL

    start = max(0.0, start)
    if duration:
        end = min(duration, end)
    return float(start), float(max(end, start + 0.5))


def tighten(start: float, end: float, silences, words: list[Word], max_pause: float) -> list[tuple[float, float]]:
    """Split [start, end] into the parts worth keeping, dropping the middle of long pauses."""
    removed = []
    for a, b in silences:
        if b - a <= max_pause or a <= start or b >= end:
            continue
        cut = (a + KEEP_AFTER, b - KEEP_BEFORE)
        if cut[1] - cut[0] < 0.15:
            continue
        # If the transcript puts a whole word in here, it is quiet speech, not a pause.
        if any(cut[0] <= w.start and w.end <= cut[1] for w in words if w.end > start and w.start < end):
            continue
        removed.append(cut)

    parts, cursor = [], start
    for a, b in removed:
        parts.append((cursor, a))
        cursor = b
    parts.append((cursor, end))
    return parts


def retime(t: float, parts: list[tuple[float, float]]) -> float:
    """Map a time in the episode to a time in the clip built from `parts`."""
    elapsed = 0.0
    for a, b in parts:
        if t < a:
            return elapsed
        if t <= b:
            return elapsed + (t - a)
        elapsed += b - a
    return elapsed
