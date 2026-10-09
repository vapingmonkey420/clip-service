"""Transcript data: timed words grouped into sentences the picker can point at."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .util import read_json, write_json

ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "etc.", "e.g.", "i.e.", "u.s.", "inc.",
    "jr.", "sr.", "no.", "prof.", "gov.", "sen.", "rep.", "a.m.", "p.m.",
}  # fmt: skip
_CLOSERS = "\"')]}”’"
_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$")


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Sentence:
    index: int
    first: int  # index of the first word
    last: int  # index of the last word, inclusive
    start: float
    end: float
    text: str


@dataclass
class Part:
    """One stretch of a long recording that was looked at closely.

    A long stream is never transcribed whole. The shortlisted stretches are laid
    end to end on a working clock, and each remembers where it came from.
    """

    index: int
    start: float  # on the working clock, where words and clips are timed
    end: float
    origin: float  # where this stretch starts on the recording's own clock
    file: str = ""  # the media file that holds it
    offset: float = 0.0  # where the stretch starts inside that file
    why: str = ""  # what the first pass saw in it


@dataclass
class Transcript:
    words: list[Word]
    sentences: list[Sentence] = field(default_factory=list)
    language: str = "en"
    duration: float = 0.0
    model: str = ""
    pauses: dict[int, float] = field(default_factory=dict)  # word index -> seconds of quiet after it
    parts: list[Part] = field(default_factory=list)  # empty when the whole recording was transcribed

    def __post_init__(self) -> None:
        if not self.sentences:
            self.sentences = build_sentences(self.words, self.pauses, breaks=self._breaks())

    def set_pauses(self, pauses: dict[int, float]) -> None:
        """Record where the audio really goes quiet, and re-split sentences with that knowledge."""
        self.pauses = dict(pauses)
        self.sentences = build_sentences(self.words, self.pauses, breaks=self._breaks())

    def _breaks(self) -> set[int]:
        """Words that end a part: a sentence may never run on from one part into the next."""
        if len(self.parts) < 2:
            return set()
        owners = [self.part_at(word.start) for word in self.words]
        return {i for i in range(len(owners) - 1) if owners[i] is not owners[i + 1]}

    def part_at(self, time: float) -> Part | None:
        """The part a moment on the working clock falls in (the nearest one, if it falls between two)."""
        if not self.parts:
            return None
        for part in self.parts:
            if part.start <= time < part.end:
                return part
        return min(self.parts, key=lambda part: min(abs(part.start - time), abs(part.end - time)))

    def origin_time(self, time: float) -> float:
        """Where a moment on the working clock sits in the original recording."""
        part = self.part_at(time)
        return time if part is None else part.origin + (time - part.start)

    def save(self, path) -> None:
        write_json(
            path,
            {
                "version": 1,
                "language": self.language,
                "duration": round(self.duration, 3),
                "model": self.model,
                "pauses": {str(i): round(length, 2) for i, length in sorted(self.pauses.items())},
                "parts": [
                    {"index": p.index, "start": round(p.start, 3), "end": round(p.end, 3), "origin": round(p.origin, 3),
                     "file": p.file, "offset": round(p.offset, 3), "why": p.why}
                    for p in self.parts
                ],
                "words": [[w.text, round(w.start, 3), round(w.end, 3)] for w in self.words],
            },
        )  # fmt: skip

    @classmethod
    def load(cls, path) -> "Transcript":
        raw = read_json(path)
        words = [Word(str(t), float(s), float(e)) for t, s, e in raw["words"]]
        return cls(
            words=clean_words(words),
            language=raw.get("language", "en"),
            duration=float(raw.get("duration", 0) or 0),
            model=raw.get("model", ""),
            pauses={int(i): float(length) for i, length in (raw.get("pauses") or {}).items()},
            parts=[
                Part(int(p["index"]), float(p["start"]), float(p["end"]), float(p["origin"]), str(p.get("file", "")),
                     float(p.get("offset", 0)), str(p.get("why", "")))
                for p in raw.get("parts") or []
            ],
        )  # fmt: skip


def clean_words(words: list[Word]) -> list[Word]:
    """Drop empties and make timings sane: ordered, non-negative, never zero length."""
    out: list[Word] = []
    previous_start = 0.0
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        start = max(float(word.start), previous_start, 0.0)
        end = max(float(word.end), start + 0.02)
        out.append(Word(text, start, end))
        previous_start = start
    return out


def is_sentence_end(text: str) -> bool:
    core = text.rstrip(_CLOSERS)
    if not core or core[-1] not in ".?!":
        return False
    return core.lower() not in ABBREVIATIONS


def build_sentences(
    words: list[Word], pauses: dict[int, float] | None = None, max_gap: float = 1.0, max_words: int = 40, breaks: set[int] | None = None
) -> list[Sentence]:
    """Split the words into units a clip can start and end on.

    A unit ends at end punctuation, at a long pause, or at a comma followed by a
    real pause. That last rule matters because speech recognition often strings
    several spoken sentences together with commas. Runaway units are cut at a comma.
    `breaks` lists words a unit must end on regardless.
    """
    pauses = pauses or {}
    breaks = breaks or set()
    spans: list[tuple[int, int]] = []
    begin = 0
    last_comma = -1
    for i, word in enumerate(words):
        soft = word.text.rstrip(_CLOSERS).endswith((",", ";", ":", "—", "–"))
        if soft:
            last_comma = i
        is_last = i == len(words) - 1
        gap = 0.0 if is_last else words[i + 1].start - word.end
        pause = pauses.get(i, 0.0)
        length = i - begin + 1
        if is_last or i in breaks or is_sentence_end(word.text) or gap >= max_gap or pause >= 0.7 or (soft and pause >= 0.3):
            spans.append((begin, i))
            begin, last_comma = i + 1, -1
        elif length >= max_words:
            cut = last_comma if last_comma - begin + 1 >= 12 else i
            spans.append((begin, cut))
            begin, last_comma = cut + 1, -1
    return [
        Sentence(
            index=n,
            first=a,
            last=b,
            start=words[a].start,
            end=words[b].end,
            text=" ".join(w.text for w in words[a : b + 1]),
        )
        for n, (a, b) in enumerate(spans)
    ]


def bare(text: str) -> str:
    """Lowercase with surrounding punctuation removed, for matching."""
    return _EDGE_PUNCT.sub("", text).lower()


def apply_replacements(words: list[Word], replace: dict) -> list[Word]:
    """Fix recurring mis-hearings, e.g. {"crowd strike": "CrowdStrike"}.

    Matching ignores case and surrounding punctuation. The replacement takes
    over the time span of the words it replaces and keeps their punctuation.
    """
    rules = []
    for wrong, right in (replace or {}).items():
        pattern = [bare(part) for part in str(wrong).split()]
        target = str(right).split()
        if pattern and all(pattern) and target:
            rules.append((pattern, target))
    if not rules:
        return words
    rules.sort(key=lambda rule: -len(rule[0]))

    keys = [bare(w.text) for w in words]
    out: list[Word] = []
    i = 0
    while i < len(words):
        for pattern, target in rules:
            n = len(pattern)
            if keys[i : i + n] != pattern:
                continue
            first, last = words[i], words[i + n - 1]
            lead = first.text[: len(first.text) - len(first.text.lstrip(".,;:!?\"'([{“‘-"))]
            stripped = last.text.rstrip(".,;:!?\"')]}”’")
            trail = last.text[len(stripped) :]
            span = (last.end - first.start) / len(target)
            for k, piece in enumerate(target):
                text = piece
                if k == 0:
                    text = lead + text
                if k == len(target) - 1:
                    text = text + trail
                out.append(Word(text, first.start + k * span, first.start + (k + 1) * span))
            i += n
            break
        else:
            out.append(words[i])
            i += 1
    return out
