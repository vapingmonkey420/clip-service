"""Command line: `python -m clipper run ...` or one stage at a time on a work folder."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

import os

from . import config, ingest, package, parts, render, silence
from . import pick as picker
from .transcript import Transcript
from .util import ClipperError, actions_notice, log, read_json, setup_logging, write_json


def load_episode(work: Path) -> dict:
    path = work / "episode.json"
    if not path.is_file():
        raise ClipperError(f"No episode.json in {work}. Start with `python -m clipper run` or the workflow.")
    return read_json(path)


def client_config(episode: dict) -> dict:
    cfg = config.load_client(episode["client"])
    cfg = config.merge(cfg, episode.get("overrides") or {})
    config.validate(cfg, "the settings for this run")
    return cfg


def parse_overrides(pairs: list[str]) -> dict:
    """['clips.count=3', 'transcribe.model=tiny.en'] -> nested dict."""
    out: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ClipperError(f"--set expects key=value, got '{pair}'.")
        key, value = pair.split("=", 1)
        node = out
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = yaml.safe_load(value)
    return out


# -------------------------------------------------------------------- stages


def stage_ingest(work: Path) -> None:
    episode = load_episode(work)
    cfg = client_config(episode)  # fail on a bad client before downloading anything
    if parts.is_long(episode, cfg):
        parts.skim(episode, cfg, work)
    else:
        ingest.prepare(episode["source"], work)


def stage_shortlist(work: Path, mode: str = "auto") -> None:
    """Long recordings only: choose the stretches that get a close look."""
    episode = load_episode(work)
    cfg = client_config(episode)
    if not parts.is_long(episode, cfg):
        log.info("Not a long recording, so there is nothing to shortlist.")
        return
    parts.choose(episode, cfg, work, mode)


def stage_transcribe(work: Path) -> None:
    episode = load_episode(work)
    cfg = client_config(episode)
    if parts.is_long(episode, cfg):
        parts.gather(episode, cfg, work)
        return
    target = work / "transcript.json"
    supplied = episode.get("transcript")
    if supplied:
        transcript = Transcript.load(supplied)
        log.info("Using the supplied transcript: %d words", len(transcript.words))
    else:
        from .transcribe import transcribe

        transcript = transcribe(read_json(work / "probe.json")["audio"], cfg["transcribe"])
    quiet = silence.find_silences(read_json(work / "probe.json")["audio"])
    transcript.set_pauses(silence.pauses_after_words(transcript.words, quiet))
    transcript.save(target)
    log.info("Transcript split into %d sentences around %d pauses", len(transcript.sentences), len(quiet))


def stage_pick(work: Path, mode: str = "auto") -> None:
    episode = load_episode(work)
    cfg = client_config(episode)
    transcript = Transcript.load(work / "transcript.json")
    picks = picker.pick(transcript, cfg, episode.get("title", ""), mode, episode.get("notes", ""))
    write_json(work / "picks.json", picks)
    log.info("Picked %d clips with %s", len(picks["clips"]), picks["picker"])
    if picks["note"]:
        log.warning(picks["note"])


def stage_render(work: Path) -> None:
    episode = load_episode(work)
    cfg = client_config(episode)
    info = read_json(work / "probe.json")
    transcript = Transcript.load(work / "transcript.json")
    picks = read_json(work / "picks.json")
    results = render.render_all(info, picks, transcript, cfg, work)
    scanned = read_json(work / "scan.json")["meta"] if info.get("long") else None
    shortlisted = read_json(work / "windows.json") if info.get("long") else None
    manifest = package.write_outputs(work, episode, picks, results, cfg, scanned, shortlisted)
    log.info("Done: %d clips in %s", len(manifest["clips"]), work / "out")


def run_check(source: str, work: Path) -> int:
    """`clipper check`: can this server reach that Twitch or Kick link?"""
    work.mkdir(parents=True, exist_ok=True)
    ok, report = parts.check_link(source, work)
    heading = "### Link check: everything worked" if ok else "### Link check: something failed"
    text = "\n".join([heading, "", f"`{source}`", "", *report])
    if not ok:
        text += (
            "\n\nIf the failure mentions bot protection, try again later; it comes and goes. If it keeps failing, "
            "the streamer can download the broadcast themselves and share the file like any other episode."
        )
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    actions_notice("Link check: everything worked" if ok else "Link check: something failed", "\n".join([source, *report]))
    return 0 if ok else 1


# ----------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="clipper", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="do everything for one episode")
    run.add_argument("--client", required=True, help="name of a file in clients/, without .yml")
    run.add_argument("--source", required=True, help="path or link to the episode")
    run.add_argument("--title", default="")
    run.add_argument("--work", type=Path, default=Path("work"))
    run.add_argument("--transcript", help="skip speech recognition and use this transcript.json")
    run.add_argument("--picker", choices=("auto", "claude", "heuristic"), default="auto")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="override a client setting for this run, e.g. --set clips.count=3")  # fmt: skip

    for name, text in (("ingest", "download and check the episode; skim it if it is a long stream"),
                       ("shortlist", "long streams only: choose the stretches to look at closely"),
                       ("transcribe", "speech to timed words"), ("pick", "choose the moments"),
                       ("render", "cut, caption and encode")):  # fmt: skip
        stage = sub.add_parser(name, help=text)
        stage.add_argument("--work", type=Path, default=Path("work"))
        if name in ("pick", "shortlist"):
            stage.add_argument("--picker", choices=("auto", "claude", "heuristic"), default="auto")

    check = sub.add_parser("check", help="test whether a Twitch or Kick link can be reached from here")
    check.add_argument("--source", required=True, help="a channel link or a link to one past broadcast")
    check.add_argument("--work", type=Path, default=Path("work"))

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if args.command == "check":
        return run_check(args.source, args.work)
    stages = {
        "ingest": ("Getting the episode", lambda: stage_ingest(args.work)),
        "shortlist": ("Shortlisting", lambda: stage_shortlist(args.work, getattr(args, "picker", "auto"))),
        "transcribe": ("Transcribing", lambda: stage_transcribe(args.work)),
        "pick": ("Choosing the moments", lambda: stage_pick(args.work, getattr(args, "picker", "auto"))),
        "render": ("Rendering", lambda: stage_render(args.work)),
    }
    doing = "Starting"
    try:
        args.work.mkdir(parents=True, exist_ok=True)
        (args.work / "error.txt").unlink(missing_ok=True)
        if args.command == "run":
            write_json(
                args.work / "episode.json",
                {
                    "client": args.client,
                    "source": args.source,
                    "title": args.title,
                    "transcript": str(Path(args.transcript).resolve()) if args.transcript else "",
                    "overrides": parse_overrides(args.set),
                },
            )
        for name in stages if args.command == "run" else [args.command]:
            doing, action = stages[name]
            action()
    except ClipperError as exc:
        log.error("%s", exc)
        (args.work / "error.txt").write_text(f"**{doing} failed.** {exc}", encoding="utf-8")
        return 1
    except Exception as exc:
        # A bug rather than a bad input: leave a short reason for the issue comment, keep the traceback in the log.
        (args.work / "error.txt").write_text(
            f"**{doing} hit an unexpected error.** `{type(exc).__name__}: {str(exc)[:300]}`", encoding="utf-8"
        )
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
