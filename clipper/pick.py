"""Choose which stretches of the episode become clips.

Claude does the choosing when a credential is available. Without one, a
rule-based fallback keeps the pipeline producing usable (if less inspired)
clips, and the result says which of the two did the work.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field

from .transcript import Sentence, Transcript
from .util import REPO, ClipperError, clock, log, run

PROMPT_FILE = REPO / "prompts" / "pick.md"
STREAM_PROMPT_FILE = REPO / "prompts" / "pick-stream.md"
MAX_PROMPT_CHARS = 450_000  # roughly 110k tokens of transcript per request
SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first": {"type": "integer"},
                    "last": {"type": "integer"},
                    "title": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["first", "last", "title", "caption", "hashtags", "why", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


@dataclass
class Pick:
    first: int  # first sentence index
    last: int  # last sentence index, inclusive
    title: str
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    why: str = ""
    score: float = 0.0


# ---------------------------------------------------------------- entry point


def pick(transcript: Transcript, cfg: dict, episode_title: str = "", mode: str = "auto", notes: str = "") -> dict:
    """Return the picks.json payload: who picked, any note, and the clips."""
    count = int(cfg["clips"]["count"])
    note = ""
    picks: list[Pick] = []
    picker = "heuristic"
    model = cfg["picker"]["model"]

    backend = claude_backend() if mode in ("auto", "claude") else None
    if mode == "claude" and backend is None:
        raise ClipperError("No Claude credential found. Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.")
    if backend:
        try:
            picks = claude_picks(transcript, cfg, episode_title, backend, model, notes)
            picker = "claude"
            if not picks:
                note = "Claude returned no usable clips, so the built-in fallback picked instead."
        except ClipperError as exc:
            if mode == "claude":
                raise
            note = f"Claude was not reachable ({str(exc)[:200]}), so the built-in fallback picked instead."
            log.warning(note)
    elif mode == "auto":
        note = "No Claude credential is set, so the built-in fallback picked these."

    if not picks:
        picker = "heuristic"
        picks = heuristic_picks(transcript, cfg)
    if not picks:
        raise ClipperError(
            "No stretch of this episode fits the clip length settings "
            f"({cfg['clips']['min_seconds']}-{cfg['clips']['max_seconds']} seconds)."
        )

    picks = picks[:count]
    sentences = transcript.sentences
    return {
        "picker": picker,
        "model": model if picker == "claude" else "",
        "note": note,
        "clips": [
            {
                **asdict(p),
                "rank": rank,
                "first_word": sentences[p.first].first,
                "last_word": sentences[p.last].last,
                "start": round(sentences[p.first].start, 3),
                "end": round(sentences[p.last].end, 3),
            }
            for rank, p in enumerate(picks, 1)
        ],
    }


# -------------------------------------------------------------------- Claude


def claude_backend() -> str | None:
    """Which way of reaching Claude is available: 'api', 'cli' or None."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "cli"
        if run(["claude", "auth", "status"], check=False, timeout=30).returncode == 0:
            return "cli"
    return None


def claude_picks(transcript: Transcript, cfg: dict, episode_title: str, backend: str, model: str, notes: str = "") -> list[Pick]:
    system = (STREAM_PROMPT_FILE if cfg.get("kind") == "stream" else PROMPT_FILE).read_text(encoding="utf-8")
    count = int(cfg["clips"]["count"])
    chunks = split_for_prompt(transcript.sentences)
    found: list[Pick] = []
    for chunk in chunks:
        # Ask for spares: some picks get dropped for length or overlap.
        share = max(2, round(count * len(chunk) / len(transcript.sentences)))
        wanted = share + max(2, share // 2)
        user = build_request(chunk, transcript, cfg, episode_title, wanted, notes)
        log.info("Asking Claude (%s) for %d moments from %d sentences", model, wanted, len(chunk))
        reply = ask(backend, system, user, model)
        found += parse_picks(reply)
    return select(found, transcript, cfg)


def split_for_prompt(sentences: list[Sentence]) -> list[list[Sentence]]:
    total = sum(len(s.text) + 16 for s in sentences)
    parts = max(1, -(-total // MAX_PROMPT_CHARS))
    size = -(-len(sentences) // parts)
    return [sentences[i : i + size] for i in range(0, len(sentences), size)]


def brief(cfg: dict) -> str:
    """What the picker is told about the show, from its client file."""

    def line(label: str, value) -> str:
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        value = " ".join(str(value or "").split())
        return f"{label}: {value}\n" if value else ""

    return (
        line("Show", cfg.get("name"))
        + line("About", cfg.get("about"))
        + line("Voice", cfg.get("voice"))
        + line("Avoid", cfg.get("avoid"))
        + line("What has worked before", cfg.get("notes"))
        + line("Usual hashtags", cfg.get("hashtags"))
    )


def build_request(chunk: list[Sentence], transcript: Transcript, cfg: dict, episode_title: str, wanted: int, notes: str = "") -> str:
    def line(label: str, value) -> str:
        value = " ".join(str(value or "").split())
        return f"{label}: {value}\n" if value else ""

    clips = cfg["clips"]
    request = (
        f"Pick the {wanted} strongest moments, best first. Each must run between "
        f"{clips['min_seconds']} and {clips['max_seconds']} seconds.\n"
        + line("Episode title", episode_title)
    )
    if transcript.parts:
        request += (
            f"This is a long recording. You are seeing {len(transcript.parts)} stretches of it that a first pass "
            "shortlisted, each under its own heading. A clip must come from a single stretch.\n"
        )
    else:
        request += f"Episode length: {clock(transcript.duration or transcript.sentences[-1].end)}\n"
    request += line("Editor's notes for this episode", notes)
    if len(chunk) < len(transcript.sentences):
        request += f"This is one part of the recording: sentences {chunk[0].index} to {chunk[-1].index}.\n"

    rows, current = [], None
    for s in chunk:
        part = transcript.part_at(s.start)
        if part is not None and part is not current:
            current = part
            why = f" First pass: {part.why}" if part.why else ""
            rows.append(f"\n--- Stretch {part.index + 1}, starting {clock(part.origin)} into the recording.{why} ---")
        rows.append(f"[{s.index}] {clock(transcript.origin_time(s.start))} {s.text}")
    lines = "\n".join(rows).strip("\n")
    return f"<brief>\n{brief(cfg)}</brief>\n\n<request>\n{request}</request>\n\n<transcript>\n{lines}\n</transcript>"


def ask_api(system: str, user: str, model: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise ClipperError("The `anthropic` package is not installed.") from None
    try:
        client = anthropic.Anthropic(max_retries=3, timeout=600)
        message = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as exc:
        raise ClipperError(f"Claude API error: {type(exc).__name__}: {str(exc)[:200]}") from None
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def ask(backend: str, system: str, user: str, model: str, schema: dict | None = None):
    """Send one request to Claude by whichever route is available."""
    return ask_api(system, user, model) if backend == "api" else ask_cli(system, user, model, schema)


def ask_cli(system: str, user: str, model: str, schema: dict | None = None):
    """One-shot call through the Claude Code CLI, which can use a Claude subscription.

    The transcript is untrusted text, so the model gets no tools, no project
    settings, and an environment stripped of everything but what the CLI needs.
    """
    keep = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "TERM", "SHELL", "SSL_CERT_FILE",
            "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
            "http_proxy", "no_proxy")  # fmt: skip
    env = {
        k: v for k, v in os.environ.items()
        if v and (k in keep or k.startswith(("CLAUDE_", "ANTHROPIC_", "LC_", "XDG_")))
    }  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="clipper-pick-") as empty:
        proc = run(
            ["claude", "-p", "--model", model, "--system-prompt", system,
             "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config",
             "--disable-slash-commands", "--no-session-persistence",
             "--output-format", "json", "--json-schema", json.dumps(schema or SCHEMA)],
            cwd=empty,
            env=env,
            input=user,
            check=False,
            timeout=900,
        )  # fmt: skip
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise ClipperError(f"Claude CLI gave no JSON (exit {proc.returncode}): {tail}") from None
    if proc.returncode != 0 or envelope.get("is_error"):
        raise ClipperError(f"Claude CLI error: {str(envelope.get('result', ''))[:300]}")
    return envelope.get("structured_output") or envelope.get("result") or ""


# ------------------------------------------------------- parsing and checking


def parse_picks(reply) -> list[Pick]:
    """Turn the model's reply (text or already-parsed JSON) into Pick objects."""
    data = reply if isinstance(reply, (dict, list)) else _extract_json(str(reply))
    if isinstance(data, dict):
        data = data.get("clips", [])
    picks = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            first, last = int(item["first"]), int(item["last"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            score = float(item.get("score", 50))
        except (TypeError, ValueError):
            score = 50.0
        tags = item.get("hashtags") if isinstance(item.get("hashtags"), list) else []
        picks.append(
            Pick(
                first=first,
                last=last,
                title=_one_line(item.get("title"), 80),
                caption=_one_line(item.get("caption"), 600),
                hashtags=clean_hashtags(tags),
                why=_one_line(item.get("why"), 400),
                score=max(0.0, min(100.0, score)),
            )
        )
    return picks


def _extract_json(text: str):
    text = text.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1).strip())
    for opener, closer in ("{}", "[]"):
        a, b = text.find(opener), text.rfind(closer)
        if 0 <= a < b:
            candidates.append(text[a : b + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ClipperError("Claude's reply was not valid JSON.")


def _one_line(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def clean_hashtags(tags, limit: int = 6) -> list[str]:
    out: list[str] = []
    for tag in tags or []:
        word = re.sub(r"[^\w]", "", str(tag), flags=re.UNICODE).lower()
        if word and f"#{word}" not in out:
            out.append(f"#{word}")
    return out[:limit]


def select(picks: list[Pick], transcript: Transcript, cfg: dict) -> list[Pick]:
    """Keep picks that point at real sentences, fit the length range and do not overlap."""
    sentences = transcript.sentences
    low, high = float(cfg["clips"]["min_seconds"]), float(cfg["clips"]["max_seconds"])
    kept: list[Pick] = []
    for p in sorted(picks, key=lambda p: -p.score):
        if not (0 <= p.first <= p.last < len(sentences)) or not p.title:
            continue
        if transcript.part_at(sentences[p.first].start) is not transcript.part_at(sentences[p.last].start):
            continue  # spans two separate stretches of the recording
        # A little over is trimmed back by whole sentences; far over or under is dropped.
        while p.last > p.first and sentences[p.last].end - sentences[p.first].start > high * 1.1:
            p.last -= 1
            p.score = max(0.0, p.score - 5)
        length = sentences[p.last].end - sentences[p.first].start
        if not low * 0.8 <= length <= high * 1.1:
            continue
        if any(p.first <= other.last and other.first <= p.last for other in kept):
            continue
        kept.append(p)
    return kept


# ------------------------------------------------------------------ fallback

_OPENERS = (
    "here's", "here is", "the thing", "nobody", "no one", "most people", "the truth", "secret",
    "mistake", "biggest", "never", "always", "the reason", "what i learned", "the problem",
    "how to", "you need", "you should", "stop", "worst", "best", "hardest", "lesson", "wrong",
    "unpopular", "surprising", "the difference", "the real", "can i ask", "what is", "what's",
)  # fmt: skip
_WEAK_STARTS = (
    "and", "but", "so", "or", "because", "yeah", "yes", "right", "okay", "ok", "um", "uh",
    "well", "like", "i mean", "you know", "exactly", "totally", "sure",
)  # fmt: skip
_HOUSEKEEPING = (
    "sponsor", "promo code", "discount code", "subscribe", "leave a review", "left a review",
    "patreon", "newsletter", "link in the", "welcome back", "thanks for listening",
    "see you next", "that is all the time", "that's all the time", "before we start",
)  # fmt: skip
_NUMBER = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million|percent)\b")


def opener_score(sentence: Sentence, pause_before: float) -> float:
    text = sentence.text.lower()
    words = text.split()
    score = 0.0
    if text.rstrip("\"')”’").endswith("?"):
        score += 3
    score += min(4, 2 * sum(1 for cue in _OPENERS if cue in text))
    if _NUMBER.search(text):
        score += 1
    plain = re.sub(r"[^\w\s']", "", text)
    if any(plain == start or plain.startswith(start + " ") for start in _WEAK_STARTS):
        score -= 3
    if len(words) < 4:
        score -= 2
    elif len(words) > 30:
        score -= 1
    if pause_before >= 0.7:
        score += 1
    return score


def heuristic_picks(transcript: Transcript, cfg: dict) -> list[Pick]:
    sentences = transcript.sentences
    low, high = float(cfg["clips"]["min_seconds"]), float(cfg["clips"]["max_seconds"])
    target = low + (high - low) * 0.4
    total = transcript.duration or (sentences[-1].end if sentences else 0)
    trim_edges = total > 600 and not transcript.parts  # on real episodes, steer away from the intro and outro
    owner = [transcript.part_at(s.start) for s in sentences]

    pauses = [sentences[i].start - sentences[i - 1].end if i else 1.0 for i in range(len(sentences))]
    openers = [opener_score(s, pauses[i]) for i, s in enumerate(sentences)]
    housekeeping = [any(cue in s.text.lower() for cue in _HOUSEKEEPING) for s in sentences]

    candidates: list[Pick] = []
    for i, head in enumerate(sentences):
        best = None
        for j in range(i, len(sentences)):
            length = sentences[j].end - head.start
            if length > high or owner[j] is not owner[i]:
                break
            if length < low:
                continue
            pause_after = pauses[j + 1] if j + 1 < len(sentences) else 1.0
            ending = (1 if sentences[j].text.rstrip("\"')”’")[-1:] in ".!" else 0) + min(2.0, pause_after * 2)
            if j + 1 < len(sentences) and openers[j + 1] >= 2:
                ending += 1
            closeness = 1 - min(1.0, abs(length - target) / max(target, 1))
            value = ending + closeness
            if best is None or value > best[0]:
                best = (value, j, length)
        if best is None:
            continue
        value, j, length = best
        words = sentences[j].last - head.first + 1
        score = openers[i] * 2 + value + min(1.5, words / max(length, 1) / 2)
        score -= 4 * sum(housekeeping[i : j + 1])
        if trim_edges and (head.start < total * 0.03 or sentences[j].end > total * 0.97):
            score -= 2
        candidates.append(
            Pick(
                first=i,
                last=j,
                title=headline(head.text),
                caption=head.text,
                hashtags=[],
                why="Picked by the built-in fallback for its opening line and clean ending.",
                score=score,
            )
        )

    kept: list[Pick] = []
    for p in sorted(candidates, key=lambda p: -p.score):
        if any(p.first <= other.last and other.first <= p.last for other in kept):
            continue
        kept.append(p)
        if len(kept) >= int(cfg["clips"]["count"]):
            break
    if kept:
        top, bottom = max(p.score for p in kept), min(p.score for p in kept)
        for p in kept:
            p.score = round(50 + 30 * (p.score - bottom) / (top - bottom), 1) if top > bottom else 60.0
    return kept


def headline(text: str, max_words: int = 7) -> str:
    """A stand-in title: the opening sentence, shortened if it runs long."""
    words = text.split()
    cut = " ".join(words[:max_words]).rstrip(".,;:\"')”’")
    if len(words) > max_words:
        cut = " ".join(words[: max_words - 1]).rstrip(".,;:!?\"')”’") + "…"
    return cut[:1].upper() + cut[1:]
