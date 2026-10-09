"""Per-client settings: clients/<slug>.yml merged over the defaults below."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

from . import streams
from .util import REPO, ClipperError

CLIENTS_DIR = REPO / "clients"
LAYOUTS = ("auto", "crop", "stack", "fit", "split")
KINDS = ("show", "stream")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULTS: dict = {
    "name": "",
    "kind": "show",  # show: talk that is transcribed whole | stream: hours of live stream, skimmed first
    "contact": "",  # who reviews the clips; used by the operator prompts, not the pipeline
    "about": "",  # what the show is and who listens
    "voice": "",  # how titles and post captions should sound
    "avoid": [],  # topics never to clip
    "notes": "",  # what has worked or flopped for this client; the picker reads it
    "feed": "",  # podcast RSS feed to watch nightly (optional)
    "twitch": "",  # Twitch channel to watch nightly for new past broadcasts (optional)
    "kick": "",  # Kick channel to watch nightly (optional)
    "permission": "",  # how you know you may clip this channel; a channel is only watched once this is filled in
    "cover": "",  # artwork for audio-only episodes, as a path inside the repo (optional)
    "hashtags": [],
    "clips": {
        "count": 8,
        "min_seconds": 20,
        "max_seconds": 60,
        "layout": "auto",  # auto | crop | stack | fit | split
        "tighten": True,  # cut dead air inside a clip
        "max_pause": 0.6,  # pauses longer than this are shortened
    },
    "captions": {
        "show": True,
        "font": "Poppins",
        "size": 84,
        "uppercase": True,
        "max_words": 3,  # words on screen at once (fewer if they would not fit on one line)
        "color": "#FFFFFF",
        "highlight": "#FFD60A",
        "outline": "#000000",
    },
    "title": {
        "show": "intro",  # intro | always | off
        "seconds": 3.5,
    },
    "transcribe": {
        "model": "small.en",
        "language": "en",
        "beam_size": 5,
        "vocabulary": [],  # names and terms the transcript should spell right
        "replace": {},  # fix-ups applied to caption text: {"crowd strike": "CrowdStrike"}
    },
    "picker": {
        "model": "claude-sonnet-5-5",
    },
    "stream": {
        "scan_model": "tiny.en",  # the quick speech model that skims a whole stream
        "max_hours": 10,  # only this much of a stream is skimmed
        "min_minutes": 15,  # the nightly check ignores broadcasts shorter than this
        "shortlist": 0,  # stretches to look at closely; 0 works it out from clips.count
        "max_height": 1080,  # the sharpest picture to fetch
        "viewer_clips": True,  # use clips viewers made as hints about where the moments are
    },
    "render": {
        "preset": "veryfast",
        "crf": 20,
    },
}

# What changes when a client file says `kind: stream`. The file itself still has the last word.
STREAM_DEFAULTS: dict = {
    "clips": {
        "min_seconds": 15,
        "tighten": False,  # cutting pauses out of gameplay makes the picture jump
    },
}


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def list_clients(root: Path = CLIENTS_DIR) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.yml") if SLUG.match(p.stem))


def load_client(slug: str, root: Path = CLIENTS_DIR) -> dict:
    slug = (slug or "").strip().lower()
    if not SLUG.match(slug):
        raise ClipperError(
            f"Client '{slug}' is not a valid name. Use lowercase letters, digits and dashes."
        )
    path = root / f"{slug}.yml"
    if not path.is_file():
        known = ", ".join(list_clients(root)) or "none yet"
        raise ClipperError(f"No client file at clients/{slug}.yml. Known clients: {known}.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ClipperError(f"clients/{slug}.yml is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ClipperError(f"clients/{slug}.yml should be a list of settings, not {type(raw).__name__}.")
    cfg = merge(merge(DEFAULTS, STREAM_DEFAULTS if raw.get("kind") == "stream" else {}), raw)
    cfg["slug"] = slug
    cfg["name"] = cfg["name"] or slug
    validate(cfg, f"clients/{slug}.yml")
    return cfg


def validate(cfg: dict, where: str = "config") -> None:
    problems: list[str] = []

    def number(section: str, key: str, low: float, high: float) -> None:
        value = cfg[section][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            problems.append(f"{section}.{key} should be a number from {low} to {high} (got {value!r})")

    number("clips", "count", 1, 30)
    number("clips", "min_seconds", 5, 170)
    number("clips", "max_seconds", 10, 180)
    number("clips", "max_pause", 0.3, 5)
    number("captions", "size", 30, 200)
    number("captions", "max_words", 1, 10)
    number("title", "seconds", 1, 30)
    number("transcribe", "beam_size", 1, 10)
    number("render", "crf", 14, 30)
    number("stream", "max_hours", 0.5, 24)
    number("stream", "min_minutes", 0, 600)
    number("stream", "shortlist", 0, 30)
    number("stream", "max_height", 360, 2160)

    clips = cfg["clips"]
    if not problems and clips["min_seconds"] >= clips["max_seconds"]:
        problems.append("clips.min_seconds should be smaller than clips.max_seconds")
    if clips["layout"] not in LAYOUTS:
        problems.append(f"clips.layout should be one of {', '.join(LAYOUTS)} (got {clips['layout']!r})")
    if cfg["title"]["show"] not in ("intro", "always", "off"):
        problems.append("title.show should be intro, always or off")
    if cfg["kind"] not in KINDS:
        problems.append(f"kind should be one of {', '.join(KINDS)} (got {cfg['kind']!r})")
    for platform in ("twitch", "kick"):
        try:
            cfg[platform] = streams.channel_name(str(cfg[platform] or ""), platform)
        except ClipperError as exc:
            problems.append(f"{platform}: {exc}")
    if not isinstance(cfg["permission"], str):
        problems.append("permission should be a line of text")
    for key in ("color", "highlight", "outline"):
        if not HEX.match(str(cfg["captions"][key])):
            problems.append(f"captions.{key} should look like '#FFD60A' (got {cfg['captions'][key]!r})")
    for key in ("avoid", "hashtags"):
        if not isinstance(cfg[key], list):
            problems.append(f"{key} should be a list")
    if not isinstance(cfg["transcribe"]["vocabulary"], list):
        problems.append("transcribe.vocabulary should be a list")
    if not isinstance(cfg["transcribe"]["replace"], dict):
        problems.append("transcribe.replace should be a mapping of wrong: right")

    if problems:
        raise ClipperError(f"Problems in {where}:\n- " + "\n- ".join(problems))
