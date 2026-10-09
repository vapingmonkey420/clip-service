"""First pass over a long recording: find the few stretches worth a close look.

Transcribing a six-hour stream properly would take two hours of machine time,
and most of a stream is not worth clipping. So the whole recording gets a fast,
rough transcript and a loudness curve, and a shortlist of stretches is chosen
from those. Only the shortlist is then fetched at full quality and transcribed
properly (see parts.py).
"""

from __future__ import annotations

import math
import os
import re
import time
import wave
from dataclasses import dataclass

import numpy as np

from . import pick as picker
from .silence import FRAME, _levels
from .util import REPO, ClipperError, clock, log

PROMPT_FILE = REPO / "prompts" / "shortlist.md"
SCHEMA = {
    "type": "object",
    "properties": {
        "windows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first": {"type": "integer"},
                    "last": {"type": "integer"},
                    "reason": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["first", "last", "reason", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["windows"],
    "additionalProperties": False,
}
RATE = 16000
CHUNK = 15 * 60  # seconds of sound handed to the speech model at a time, which bounds memory on any length of stream
PAD_BEFORE, PAD_AFTER = 20.0, 12.0  # extra kept around a shortlisted stretch, so the close look can find clean edges
MAX_WINDOW = 300.0
MAX_SHORTLIST_SECONDS = 45 * 60


@dataclass
class Line:
    """One phrase of the rough transcript."""

    index: int
    start: float
    end: float
    text: str


# ----------------------------------------------------------------- loudness


def loudness(wav_path) -> np.ndarray:
    """How loud each second of the recording gets, in dBFS."""
    levels = _levels(wav_path)
    per = int(round(1 / FRAME))
    usable = levels.size - levels.size % per
    if usable == 0:
        return np.zeros(0, dtype=np.float32)
    return np.percentile(levels[:usable].reshape(-1, per), 90, axis=1).astype(np.float32)


def loud_moments(per_second: np.ndarray, limit: int = 12) -> list[dict]:
    """Stretches much louder than both ordinary talking and their own surroundings.

    Shouting, laughter and big moments in a game stand out this way. So does a
    burst of game noise, which is why these only ever count as hints.
    Returns [{at, end, lift}] in time order; lift is dB above ordinary talking.
    """
    count = per_second.size
    if count < 60:
        return []
    typical = float(np.percentile(per_second, 85))  # about as loud as ordinary talking gets
    if typical - float(np.percentile(per_second, 15)) < 6:
        return []  # one steady level throughout: nothing stands out
    half = min(150, count // 2)
    padded = np.pad(per_second, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1)
    around = np.concatenate([np.median(windows[i : i + 4000], axis=1) for i in range(0, count, 4000)])[:count]
    loud = np.flatnonzero(per_second >= np.maximum(around + 7.0, typical + 3.5))

    events: list[list[int]] = []
    for second in loud:
        if events and second - events[-1][1] <= 3:
            events[-1][1] = int(second)
        else:
            events.append([int(second), int(second)])
    scored = []
    for a, b in events:
        lift = float(per_second[a : b + 1].max() - typical)
        if b - a + 1 >= 2 or lift >= 8:
            scored.append((lift + min(6, b - a + 1), a, b, lift))
    kept: list[tuple] = []
    for item in sorted(scored, reverse=True):
        if all(abs(item[1] - other[1]) >= 20 for other in kept):
            kept.append(item)
        if len(kept) >= limit:
            break
    return [{"at": float(a), "end": float(b + 1), "lift": round(lift, 1)} for _, a, b, lift in sorted(kept, key=lambda k: k[1])]


# ---------------------------------------------------------- rough transcript


def rough_transcript(
    wav_path, settings: dict, language: str = "en", per_second: np.ndarray | None = None, max_seconds: float | None = None
) -> list[Line]:
    """A fast, approximate transcript of the recording (or its first `max_seconds`), phrase by phrase."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ClipperError("Transcription needs the `faster-whisper` package (pip install -r requirements.txt).") from None

    name = settings["scan_model"]
    log.info("Loading the quick speech model %s", name)
    try:
        model = WhisperModel(name, device="auto", compute_type="int8", cpu_threads=os.cpu_count() or 2)
    except Exception as exc:  # download and load errors come from several libraries
        raise ClipperError(
            f"Could not load the speech model '{name}' ({type(exc).__name__}: {str(exc)[:200]}). "
            "The first run downloads it from Hugging Face, so check the network, or pick another stream.scan_model."
        ) from None

    lines: list[Line] = []
    started = time.monotonic()
    with wave.open(str(wav_path), "rb") as fh:
        if (fh.getnchannels(), fh.getsampwidth(), fh.getframerate()) != (1, 2, RATE):
            raise ClipperError(f"{wav_path} is not 16 kHz mono 16-bit audio.")
        total = fh.getnframes()
        if max_seconds is not None:
            total = min(total, int(max_seconds * RATE))
        position = 0
        while position < total:
            frames = min(CHUNK * RATE, total - position)
            if position + frames < total and per_second is not None:
                # End the chunk on the quietest of its last twenty seconds, so a sentence is not cut in half.
                edge = (position + frames) // RATE
                tail = per_second[max(0, edge - 20) : edge]
                if tail.size:
                    frames = (max(0, edge - 20) + int(np.argmin(tail))) * RATE + RATE // 2 - position
            fh.setpos(position)
            samples = np.frombuffer(fh.readframes(frames), dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = model.transcribe(
                samples,
                language=language or None,
                beam_size=1,
                best_of=1,
                temperature=0.0,  # one quick attempt per stretch; no slower retries
                word_timestamps=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 700},
                condition_on_previous_text=False,
            )
            offset = position / RATE
            for segment in segments:
                text = " ".join((segment.text or "").split())
                if not text or segment.avg_logprob < -1.3 or (segment.no_speech_prob > 0.85 and segment.avg_logprob < -0.8):
                    continue  # the model was guessing, which it does over music and game noise
                if lines and text == lines[-1].text:
                    continue  # a stuck model repeats one phrase
                # The model's end times are dependable. Its start times are not: after a silence it
                # reports the start as where the last phrase ended. So a phrase is never allowed to
                # be longer than its words could take to say.
                end = offset + float(segment.end)
                start = max(offset + float(segment.start), end - (0.45 * len(text.split()) + 1.5))
                lines.append(Line(len(lines), start, end, text))
            position += frames
            done = position / RATE
            log.info("Skimmed %s of %s (%.0fx real time)", clock(done), clock(total / RATE), done / max(time.monotonic() - started, 1e-6))
    return lines


def lines_from_transcript(transcript) -> list[Line]:
    """Use an existing transcript as the rough one (for tests and re-runs)."""
    return [Line(n, s.start, s.end, s.text) for n, s in enumerate(transcript.sentences)]


# ------------------------------------------------------------------ shortlist


def wanted_count(cfg: dict) -> int:
    """How many stretches to look at closely: more than the clips wanted, since some will disappoint."""
    fixed = int(cfg["stream"].get("shortlist") or 0)
    return fixed if fixed > 0 else min(24, max(5, round(int(cfg["clips"]["count"]) * 1.5) + 2))


def shortlist(lines: list[Line], loud: list[dict], hints: list[dict], cfg: dict, meta: dict, mode: str = "auto") -> dict:
    """Choose the stretches to look at closely. Returns {picker, model, note, windows: [{start, end, why, score}]}."""
    total = float(meta.get("duration") or (lines[-1].end if lines else 0))
    if sum(len(line.text.split()) for line in lines) < 20:
        raise ClipperError("Almost no speech was found in this recording, so there is nothing to clip.")
    wanted = wanted_count(cfg)
    model = cfg["picker"]["model"]
    who, note, raw = "heuristic", "", []

    backend = picker.claude_backend() if mode in ("auto", "claude") else None
    if mode == "claude" and backend is None:
        raise ClipperError("No Claude credential found. Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.")
    if backend:
        try:
            raw = claude_shortlist(lines, loud, hints, cfg, meta, wanted, backend, model)
            who = "claude"
            if not raw:
                note = "Claude shortlisted nothing usable in the first pass, so the built-in fallback chose where to look."
        except ClipperError as exc:
            if mode == "claude":
                raise
            note = f"Claude was not reachable for the first pass ({str(exc)[:200]}), so the built-in fallback chose where to look."
            log.warning(note)
    elif mode == "auto":
        note = "No Claude credential is set, so the built-in fallback chose which stretches to look at."
    if not raw:
        who, raw = "heuristic", heuristic_shortlist(lines, loud, hints, cfg, wanted)

    windows = to_windows(raw, total, cfg, wanted)
    if not windows:
        raise ClipperError("Nothing in this recording looked worth a close look: too little is said in any one stretch.")
    return {"picker": who, "model": model if who == "claude" else "", "note": note, "windows": windows}


def tags(lines: list[Line], loud: list[dict], hints: list[dict]) -> dict[int, str]:
    """Marks added to transcript lines: where it got loud, and where viewers made clips."""
    out: dict[int, list[str]] = {}
    for event in loud:
        for line in lines:
            if line.start <= event["end"] + 1 and line.end >= event["at"] - 1:
                out.setdefault(line.index, [])
                if "{LOUD}" not in out[line.index]:
                    out[line.index].append("{LOUD}")
    views: dict[int, int] = {}
    for hint in hints:
        at = float(hint["at"])

        def distance(line: Line) -> float:
            return 0.0 if line.start <= at <= line.end else min(abs(line.start - at), abs(line.end - at))

        near = min(lines, key=distance, default=None)
        if near is not None and distance(near) <= 45:
            views[near.index] = views.get(near.index, 0) + int(hint.get("views") or 0)
    for index, count in views.items():
        out.setdefault(index, []).append(f"{{CLIPPED {count}}}")
    return {index: " ".join(marks) for index, marks in out.items()}


def build_request(chunk: list[Line], lines: list[Line], marks: dict[int, str], cfg: dict, meta: dict, wanted: int) -> str:
    clips = cfg["clips"]

    def line(label: str, value) -> str:
        value = " ".join(str(value or "").split())
        return f"{label}: {value}\n" if value else ""

    games = "; ".join(f"{clock(at)} {name}" for at, name in meta.get("games") or [])
    request = (
        f"Shortlist up to {wanted} stretches, best first. The clips cut from them will each run "
        f"{clips['min_seconds']} to {clips['max_seconds']} seconds.\n"
        + line("Recording", meta.get("title"))
        + f"Length: {clock(float(meta.get('duration') or lines[-1].end))}\n"
        + line("What was on, by time", games)
        + line("Editor's notes for this recording", meta.get("notes"))
    )
    if len(chunk) < len(lines):
        request += f"This is one part of the recording: lines {chunk[0].index} to {chunk[-1].index}.\n"
    body = "\n".join(f"[{l.index}] {clock(l.start)} {l.text}" + (f" {marks[l.index]}" if l.index in marks else "") for l in chunk)
    return f"<brief>\n{picker.brief(cfg)}</brief>\n\n<request>\n{request}</request>\n\n<transcript>\n{body}\n</transcript>"


def claude_shortlist(lines, loud, hints, cfg, meta, wanted: int, backend: str, model: str) -> list[dict]:
    system = PROMPT_FILE.read_text(encoding="utf-8")
    marks = tags(lines, loud, hints)
    size = sum(len(line.text) + 20 for line in lines)
    pieces = max(1, -(-size // picker.MAX_PROMPT_CHARS))
    step = -(-len(lines) // pieces)
    raw: list[dict] = []
    for i in range(0, len(lines), step):
        chunk = lines[i : i + step]
        share = max(3, round(wanted * len(chunk) / len(lines)) + 2)
        log.info("Asking Claude (%s) to shortlist %d stretches from %d lines", model, share, len(chunk))
        reply = picker.ask(backend, system, build_request(chunk, lines, marks, cfg, meta, share), model, SCHEMA)
        raw += parse_windows(reply, lines)
    return raw


def parse_windows(reply, lines: list[Line]) -> list[dict]:
    """Turn Claude's reply into stretches on the recording's clock, dropping anything malformed."""
    data = reply if isinstance(reply, (dict, list)) else picker._extract_json(str(reply))
    if isinstance(data, dict):
        data = data.get("windows", [])
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            first, last = int(item["first"]), int(item["last"])
            score = float(item.get("score", 50))
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= first <= last < len(lines):
            continue
        start, end = lines[first].start, lines[last].end
        out.append({
            "start": start, "end": min(end, start + MAX_WINDOW), "score": max(0.0, min(100.0, score)),
            "why": picker._one_line(item.get("reason"), 240),
        })  # fmt: skip
    return out


# ------------------------------------------------------------------ fallback

_EXCITED = re.compile(
    r"\b(no way|oh my god|oh my gosh|what the|let'?s go|insane|crazy|clip (that|it)|holy|are you kidding|"
    r"i can'?t believe|unbelievable|best|worst|never|honest(ly)?|story|happened|i have to tell|secret|hate|love|"
    r"wait wait|did you see|how did)\b"
)
_DULL = re.compile(
    r"\b(thanks? (you )?for the (follow|sub|raid|bits|gift)|welcome in|be right back|brb|my settings|can you hear me|"
    r"is the audio|loading (in|into)|give me a second|setting up|see you (tomorrow|next))\b"
)


def heuristic_shortlist(lines: list[Line], loud: list[dict], hints: list[dict], cfg: dict, wanted: int) -> list[dict]:
    """Without Claude: favour stretches with lively talk, loud moments and viewer clips."""
    span = float(cfg["clips"]["max_seconds"]) + 20.0
    candidates = []
    for i, head in enumerate(lines):
        inside = [line for line in lines[i:] if line.end <= head.start + span]
        if not inside:
            continue
        text = " ".join(line.text for line in inside).lower()
        words = len(text.split())
        if words < 12:
            continue
        end = inside[-1].end
        score = 2.0 * min(2.5, words / span)
        score += 0.6 * min(6, text.count("!") + text.count("?"))
        score += 1.0 * min(5, len(_EXCITED.findall(text)))
        score -= 3.0 * len(_DULL.findall(text))
        events = [e for e in loud if e["at"] <= end and e["end"] >= head.start]
        score += sum(2.0 + e["lift"] / 4 for e in events[:2])
        nearby = [h for h in hints if head.start - 15 <= h["at"] <= end + 15]
        if nearby:
            score += min(4.0, 1.5 * math.log10(sum(h["views"] for h in nearby) + 10))
        reasons = ["lively talk"] + (["a loud moment"] if events else []) + (["viewer clips nearby"] if nearby else [])
        candidates.append({"start": head.start, "end": end, "score": score, "why": "Chosen by the built-in fallback: " + ", ".join(reasons) + "."})

    kept: list[dict] = []
    for item in sorted(candidates, key=lambda c: -c["score"]):
        if any(item["start"] < other["end"] + PAD_AFTER and other["start"] - PAD_BEFORE < item["end"] for other in kept):
            continue
        kept.append(item)
        if len(kept) >= wanted:
            break
    if kept:
        top, bottom = max(k["score"] for k in kept), min(k["score"] for k in kept)
        for item in kept:
            item["score"] = round(50 + 30 * (item["score"] - bottom) / (top - bottom), 1) if top > bottom else 60.0
    return kept


# -------------------------------------------------------------------- windows


def to_windows(raw: list[dict], total: float, cfg: dict, wanted: int) -> list[dict]:
    """Pad the chosen stretches, keep them inside the recording, and merge any that touch."""
    high = float(cfg["clips"]["max_seconds"])
    shortest = min(high + 30.0, MAX_WINDOW)  # room for a full-length clip with clean edges
    padded = []
    for item in sorted(raw, key=lambda w: -w["score"])[:wanted]:
        a, b = max(0.0, item["start"] - PAD_BEFORE), min(total, item["end"] + PAD_AFTER)
        short = shortest - (b - a)
        if short > 0:
            a = max(0.0, a - short / 2)
            b = min(total, a + shortest)
            a = max(0.0, b - shortest)
        if b - a >= 10:
            padded.append({"start": a, "end": min(b, a + MAX_WINDOW), "why": item["why"], "score": item["score"]})

    merged: list[dict] = []
    for item in sorted(padded, key=lambda w: w["start"]):
        last = merged[-1] if merged else None
        if last and item["start"] <= last["end"] + 8:
            if item["end"] - last["start"] <= MAX_WINDOW:
                last["end"] = max(last["end"], item["end"])
                last["why"] = " / ".join(dict.fromkeys([last["why"], item["why"]]))[:400]
                last["score"] = max(last["score"], item["score"])
                continue
            item = {**item, "start": last["end"]}
            if item["end"] - item["start"] < 30:
                continue
        merged.append(item)

    while merged and sum(w["end"] - w["start"] for w in merged) > MAX_SHORTLIST_SECONDS:
        merged.remove(min(merged, key=lambda w: w["score"]))
    return [{"start": round(w["start"], 2), "end": round(w["end"], 2), "why": w["why"], "score": w["score"]} for w in merged]
