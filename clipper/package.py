"""Write what goes out with the clips: a manifest and a review sheet with ready-to-paste captions."""

from __future__ import annotations

from pathlib import Path

from . import streams
from .pick import clean_hashtags
from .util import clock, write_json


def picker_line(manifest: dict) -> str:
    who = f"Claude ({manifest['model']})" if manifest["picker"] == "claude" else "the built-in fallback picker"
    return f"Picked by {who}."


def post_text(clip: dict, cfg: dict) -> str:
    """Caption plus hashtags, the client's standing ones first."""
    tags = clean_hashtags(list(cfg.get("hashtags") or []) + list(clip.get("hashtags") or []), limit=8)
    return "\n\n".join(part for part in (clip.get("caption", "").strip(), " ".join(tags)) if part)


def write_outputs(workdir: Path, episode: dict, picks: dict, results: list[dict], cfg: dict, recording: dict | None = None,
                  shortlisted: dict | None = None) -> dict:  # fmt: skip
    """`recording` and `shortlisted` are given for a long recording: its details, and the first pass's result."""
    outdir = workdir / "out"
    for clip in results:
        clip["post"] = post_text(clip, cfg)
        clip["watch"] = streams.moment_url(episode.get("source", ""), clip["source_start"])
    notes = [picks.get("note", "")]
    manifest = {
        "client": cfg["slug"],
        "show": cfg["name"],
        "title": episode.get("title", ""),
        "source": episode.get("source", ""),
        "picker": picks["picker"],
        "model": picks.get("model", ""),
        "clips": results,
    }
    if recording is not None:
        manifest["recording"] = {
            "platform": recording.get("platform", ""),
            "channel": recording.get("channel", ""),
            "length": recording.get("full_duration", 0),
            "skimmed": recording.get("duration", 0),
            "stretches": len((shortlisted or {}).get("windows", [])),
        }
        notes.append((shortlisted or {}).get("note", ""))
        if recording.get("full_duration", 0) > recording.get("duration", 0) + 60:
            notes.append(f"Only the first {clock(recording['duration'])} of this {clock(recording['full_duration'])} stream was looked at.")
    manifest["note"] = " ".join(note for note in notes if note)
    write_json(outdir / "manifest.json", manifest)
    (outdir / "clips.md").write_text(review_sheet(manifest), encoding="utf-8")
    return manifest


def review_sheet(manifest: dict) -> str:
    heading = " — ".join(part for part in (manifest["show"], manifest["title"]) if part)
    count = len(manifest["clips"])
    lines = [f"# {heading}", "", f"{count} clip{'s' if count != 1 else ''}, best first. {picker_line(manifest)}"]
    recording = manifest.get("recording")
    whole = "stream" if recording else "episode"
    if recording:
        lines += ["", f"From a {clock(recording['length'])} stream. {recording['stretches']} stretches of it were looked at closely."]
    if manifest.get("note"):
        lines += ["", f"> {manifest['note']}"]
    for clip in manifest["clips"]:
        where = f"from {clock(clip['source_start'])} to {clock(clip['source_end'])} of the {whole}"
        if clip.get("watch"):
            where += f" ([watch it there]({clip['watch']}))"
        facts = [f"`{clip['file']}`", clock(clip["duration"]), where]
        if clip["trimmed"] >= 0.5:
            facts.append(f"{clip['trimmed']:.1f}s of pauses removed")
        lines += ["", f"## {clip['rank']}. {clip['title']}", "", " · ".join(facts), "", "**Post text**", "", clip["post"]]
        if clip.get("why"):
            lines += ["", f"**Why this one:** {clip['why']}"]
        lines += ["", f"**What is said:** {clip['text']}"]
    return "\n".join(lines) + "\n"
