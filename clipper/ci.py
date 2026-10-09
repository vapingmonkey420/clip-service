"""Glue between GitHub and the pipeline. Issues are the queue; labels are the state.

    episode      someone asked for clips
    processing   a run has picked it up
    clipped      clips are attached to a release and linked in a comment
    failed       something went wrong; the comment says what

Everything talks to GitHub through the `gh` CLI, which the workflow already has.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import time

from . import config, feeds, package, streams
from .util import ClipperError, clock, log, read_json, run, setup_logging, write_json

LABELS = {
    "episode": ("1d76db", "An episode to cut into clips"),
    "processing": ("fbca04", "Clips are being made"),
    "clipped": ("0e8a16", "Clips are ready"),
    "failed": ("d73a4a", "Clipping failed; see the comment"),
    # The workflow never reads these three. They exist so whoever operates the service can use them.
    "delivered": ("5319e7", "Clips were sent to the client"),
    "client": ("c5def5", "Details for a new client file"),
    "results": ("bfdadc", "How posted clips performed"),
}
NOT_REAL_CLIENTS = {"demo", "demo-stream", "spec"}
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
BOT = "github-actions[bot]"
MAX_PER_RUN = 6
BROADCASTS_PER_CHECK = 2  # newest past broadcasts looked at per channel each night
BROADCAST_MAX_AGE = 4 * 86400  # so adding a channel never back-fills its whole archive
URL = re.compile(r"https?://[^\s<>\"')\]]+")
EMPTY = {"", "_no response_", "none", "n/a"}


def gh(*args: str, check: bool = True, input: str | None = None) -> str:
    return run(["gh", *args], check=check, input=input, timeout=1800).stdout


def repo() -> str:
    name = os.environ.get("GITHUB_REPOSITORY", "")
    if not name:
        raise ClipperError("GITHUB_REPOSITORY is not set; this command is meant to run inside GitHub Actions.")
    return name


def set_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    log.info("%s=%s", name, value)


def ensure_labels() -> None:
    for name, (colour, description) in LABELS.items():
        gh("label", "create", name, "--color", colour, "--description", description, "--force", check=False)


def label_names(issue: dict) -> set[str]:
    return {label["name"] if isinstance(label, dict) else str(label) for label in issue.get("labels", [])}


# ------------------------------------------------------------ reading issues


def parse_issue(title: str, body: str, labels=()) -> dict:
    """Pull the request out of an issue, whether it came from the form or was typed freehand.

    Accepted, in order: the form's "### Heading" blocks, "key: value" lines, a
    `client:<name>` label, and failing all that the first link in the text.
    """
    body = (body or "").replace("\r\n", "\n")
    fields: dict[str, str] = {}
    for block in re.split(r"(?m)^#{2,4}\s+", body)[1:]:
        heading, _, value = block.partition("\n")
        fields[heading.strip().lower()] = value.strip()
    for match in re.finditer(r"(?mi)^\s*\**([a-z][a-z ]{1,24}?)\**\s*:\s*(\S.*)$", body):
        fields.setdefault(match.group(1).strip().lower(), match.group(2).strip().strip("*").strip())

    def first(*names: str) -> str:
        for name in names:
            for key, value in fields.items():
                if key == name or key.startswith(name):
                    cleaned = value.strip()
                    if cleaned.lower() not in EMPTY:
                        return cleaned
        return ""

    client = first("client", "show").strip("`").lower()
    if not client:
        client = next((label.split(":", 1)[1].strip().lower() for label in labels if label.lower().startswith("client:")), "")
    link_text = first("episode link", "link", "url", "source", "file", "episode file")
    link = URL.search(link_text) or URL.search(body)
    count_text = first("number of clips", "clips", "count")
    count = int(count_text) if count_text.isdigit() and 1 <= int(count_text) <= 30 else None
    return {
        "client": client,
        "source": link.group(0).rstrip(".,;") if link else "",
        # The form pre-fills the issue title with "Episode: "; left like that, it says nothing.
        "title": first("episode title", "title") or re.sub(r"(?i)^episode:?\s*$", "", (title or "").strip()),
        "notes": first("notes", "note", "focus"),
        "count": count,
    }


def resolve_client(name: str) -> str:
    """Use the named client, or the only real one when none was named."""
    if name:
        return name
    real = [slug for slug in config.list_clients() if slug not in NOT_REAL_CLIENTS]
    if len(real) == 1:
        return real[0]
    known = ", ".join(real) or "none yet; add one in clients/"
    raise ClipperError(f"The issue does not say which client this is for. Known clients: {known}.")


def trusted(issue: dict) -> bool:
    """Only collaborators may spend this repo's minutes. The workflow's own issues (from feeds) count too."""
    author = (issue.get("user") or {}).get("login", "")
    return issue.get("author_association") in TRUSTED or author == BOT


def looks_like_request(issue: dict) -> bool:
    """True for an issue written with the form's fields even though it carries no label.

    GitHub only applies a form's label if that label already exists, and a tool
    filing issues for you may forget it. The "Episode link" field is the tell.
    """
    body = issue.get("body") or ""
    return bool(re.search(r"(?mi)^\s*(#{2,4}\s*|\**)episode (link|file)", body)) and bool(URL.search(body))


def eligible(issue: dict) -> bool:
    names = label_names(issue)
    return (
        not issue.get("pull_request")
        and issue.get("state", "open") == "open"
        and ("episode" in names or looks_like_request(issue))
        and "clipped" not in names
        and trusted(issue)
    )


# ---------------------------------------------------------------------- plan


def cmd_plan(args) -> None:
    """Decide which issues this run handles and print them for the job matrix."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    payload = read_json(path) if path and Path(path).is_file() else {}
    ensure_labels()

    numbers: list[int] = []
    if event == "issues":
        issue = payload.get("issue", {})
        if eligible(issue):
            numbers = [int(issue["number"])]
            if "episode" not in label_names(issue):
                gh("issue", "edit", str(issue["number"]), "--add-label", "episode", check=False)
        else:
            log.info("Issue #%s is not an episode request from a collaborator; nothing to do.", issue.get("number"))
    else:
        inputs = payload.get("inputs") or {}
        if str(inputs.get("issue") or "").strip().isdigit():
            number = int(inputs["issue"])
            # Asking for a specific issue is how a person says "do it again", so clear its old state.
            gh("issue", "edit", str(number), "--add-label", "episode", "--remove-label", "clipped",
               "--remove-label", "failed", "--remove-label", "processing", check=False)  # fmt: skip
            numbers = [number]
        elif (inputs.get("client") or "").strip() and (inputs.get("source") or "").strip():
            numbers = [create_issue(inputs["client"].strip(), inputs["source"].strip(), (inputs.get("title") or "").strip())]
        else:
            numbers = new_from_feeds() + new_from_channels() + pending()
    numbers = list(dict.fromkeys(numbers))[:MAX_PER_RUN]
    set_output("issues", json.dumps(numbers))
    set_output("count", str(len(numbers)))


def pending() -> list[int]:
    """Open episode requests that have neither finished nor failed, oldest first."""
    listing = gh(
        "api", "--paginate", f"repos/{repo()}/issues?labels=episode&state=open&per_page=100&sort=created&direction=asc",
        "--jq", ".[] | {number, state, author_association, user: {login: .user.login}, "
                "pull_request: (.pull_request != null), labels: [.labels[].name]}",
    )  # fmt: skip
    issues = [json.loads(line) for line in listing.splitlines() if line.strip()]
    return [int(i["number"]) for i in issues if eligible(i) and "failed" not in label_names(i)]


def create_issue(client: str, source: str, title: str, marker: str = "", kind: str = "feed") -> int:
    cfg = config.load_client(client)
    body = f"### Client\n\n{client}\n\n### Episode link\n\n{source}\n\n### Episode title\n\n{title or '_No response_'}\n"
    if marker:
        # An invisible note in the issue, which is how the nightly run knows it has already filed this one.
        body += f"\n<!-- {kind}-item:{marker} -->\n"
    heading = f"{cfg['name']}: {title}" if title else f"{cfg['name']}: new episode"
    out = gh("issue", "create", "--title", heading[:240], "--label", "episode", "--body", body)
    match = re.search(r"/issues/(\d+)", out)
    if not match:
        raise ClipperError(f"Could not create an issue for {client}: {out.strip()[:200]}")
    return int(match.group(1))


def new_from_feeds() -> list[int]:
    """Open an issue for each fresh episode in each client's feed that does not have one yet."""
    watched = []
    for slug in config.list_clients():
        try:
            cfg = config.load_client(slug)
        except ClipperError as exc:
            log.warning("Skipping %s: %s", slug, str(exc).splitlines()[0])
            continue
        if cfg.get("feed"):
            watched.append((slug, cfg["feed"]))
    if not watched:
        return []

    existing = gh("api", "--paginate", f"repos/{repo()}/issues?labels=episode&state=all&per_page=100", "--jq", ".[].body")
    seen = set(re.findall(r"feed-item:([0-9a-f]{16})", existing))
    created = []
    for slug, feed in watched:
        try:
            fresh = feeds.recent(feeds.parse_feed(feeds.fetch_feed(feed)))
        except ClipperError as exc:
            log.warning("Feed for %s: %s", slug, exc)
            continue
        for episode in fresh:
            if episode["key"] in seen:
                continue
            created.append(create_issue(slug, episode["source"], episode["title"], episode["key"]))
            log.info("New episode for %s: %s", slug, episode["title"])
    return created


def new_from_channels(now: float | None = None) -> list[int]:
    """Open an issue for each finished past broadcast on a watched Twitch or Kick channel that has none yet."""
    now = now or time.time()
    named = []
    for slug in config.list_clients():
        try:
            cfg = config.load_client(slug)
        except ClipperError as exc:
            log.warning("Skipping %s: %s", slug, str(exc).splitlines()[0])
            continue
        named += [(slug, cfg, platform, cfg[platform]) for platform in ("twitch", "kick") if cfg.get(platform)]
    if not named:
        return []

    open_notes = gh("api", "--paginate", f"repos/{repo()}/issues?labels=failed&state=open&per_page=100", "--jq", ".[].body", check=False)
    existing = gh("api", "--paginate", f"repos/{repo()}/issues?labels=episode&state=all&per_page=100", "--jq", ".[].body")
    seen = set(re.findall(r"stream-item:([\w-]+)", existing))
    created = []
    for slug, cfg, platform, channel in named:
        site = platform.title()
        if not str(cfg.get("permission") or "").strip():
            log.warning("Not watching %s on %s: clients/%s.yml has no `permission` line.", channel, site, slug)
            notice(
                f"permission-{slug}", f"Not watching {channel} on {site} yet", open_notes,
                f"`clients/{slug}.yml` names the {site} channel **{channel}** but has no `permission` line, so the nightly "
                "check is leaving it alone.\n\nAdd a line saying how you know you may clip this channel, for example "
                "`permission: Client agreement signed 2026-10-12` or `permission: Clipping program rules: <link>`. "
                "Then close this issue.",
            )  # fmt: skip
            continue
        try:
            listing = streams.recent(platform, channel, limit=BROADCASTS_PER_CHECK)
        except ClipperError as exc:
            log.warning("Could not check %s on %s: %s", channel, site, str(exc)[:300])
            notice(
                f"check-{platform}-{channel}", f"Could not check {channel} on {site}", open_notes,
                f"The nightly check could not list past broadcasts for **{channel}** on {site}, so nothing new was filed "
                f"for `{slug}`.\n\n> {str(exc)[:600]}\n\nTo see which step fails, run the link check: Actions → Clips → "
                f"Run workflow → paste `{streams.channel_url(platform, channel)}` into \"only test a Twitch or Kick link\". "
                "If this was a one-off, close this issue. While it stays open, no second one is opened for this channel.",
            )  # fmt: skip
            continue
        for item in listing:
            if item["live"] or f"{platform}-{re.sub(r'[^A-Za-z0-9]', '', item['id'])[:40]}" in seen:
                continue
            try:
                # The listing alone does not say whether a Twitch broadcast is still being recorded, or when it began.
                broadcast = streams.resolve(item["url"])
            except ClipperError as exc:
                log.warning("Could not read %s: %s", item["url"], str(exc)[:300])
                continue
            length = broadcast.duration or item["duration"]
            if broadcast.live or broadcast.key in seen:
                continue
            if length and length < float(cfg["stream"]["min_minutes"]) * 60:
                continue
            if broadcast.started and now - broadcast.started - length > BROADCAST_MAX_AGE:
                continue
            title = broadcast.title or item["title"] or "Stream"
            created.append(create_issue(slug, item["url"], title, broadcast.key, kind="stream"))
            seen.add(broadcast.key)
            log.info("New broadcast for %s: %s", slug, title)
    return created


def notice(key: str, title: str, open_notes: str, body: str) -> None:
    """Raise a problem with the nightly check as an issue, so it is seen without anybody reading logs.

    One per problem: while an issue carrying the same key is open, no other is opened.
    """
    marker = f"notice:{key}"
    if marker in open_notes:
        return
    gh("issue", "create", "--title", title[:240], "--label", "failed", "--body", f"{body}\n\n<!-- {marker} -->\n", check=False)


# --------------------------------------------------------------------- start


def cmd_start(args) -> None:
    """Turn the issue into work/episode.json and mark it as in progress."""
    issue = json.loads(gh("api", f"repos/{repo()}/issues/{args.issue}"))
    names = label_names(issue)
    if names & {"clipped", "failed"}:
        log.info("Issue #%s is already %s; skipping.", args.issue, "done" if "clipped" in names else "marked failed")
        set_output("skip", "true")
        return
    args.work.mkdir(parents=True, exist_ok=True)
    if "processing" in names:
        # An earlier run died without reporting (most likely it ran out of time). Retrying
        # by itself every night could quietly burn through the month's minutes, so stop here.
        write_json(args.work / "episode.json", {"issue": int(args.issue)})
        raise ClipperError(
            "An earlier run on this episode stopped without finishing, most likely because it "
            "ran out of time. It was not retried automatically. If the episode is very long, "
            "try a shorter cut of it or a faster transcribe.model for this client."
        )
    request = parse_issue(issue.get("title", ""), issue.get("body") or "", label_names(issue))
    # Saved first, so that a failure below can still be reported on the right issue.
    episode = {"issue": int(args.issue), "client": request["client"], "source": request["source"], "title": request["title"]}
    write_json(args.work / "episode.json", episode)

    episode["client"] = resolve_client(request["client"])
    cfg = config.load_client(episode["client"])
    if not episode["source"]:
        raise ClipperError("The issue has no link to the episode file. Add one and reopen the issue.")
    overrides: dict = {}
    if request["count"]:
        overrides.setdefault("clips", {})["count"] = request["count"]
    if request["notes"]:
        episode["notes"] = request["notes"]
    episode["overrides"] = overrides
    write_json(args.work / "episode.json", episode)
    gh("issue", "edit", str(args.issue), "--add-label", "processing", "--remove-label", "failed", check=False)
    set_output("skip", "false")
    # Names the speech models this run will load, so the workflow can keep them between runs.
    models = [str(cfg["transcribe"]["model"])]
    if streams.is_long(episode["source"], cfg):
        models.append(str(cfg["stream"]["scan_model"]))
    set_output("model", "+".join(dict.fromkeys(models)))


# ------------------------------------------------------------------- publish


def cmd_publish(args) -> None:
    """Attach the clips to a release, link them in a comment, and mark the issue done."""
    out = args.work / "out"
    manifest = read_json(out / "manifest.json")
    tag = f"clips-{args.issue}"
    heading = " — ".join(part for part in (manifest["show"], manifest["title"]) if part)
    files = sorted(out.glob("*.mp4")) + [p for p in (out / "clips.md", out / "contact-sheet.jpg", out / "manifest.json") if p.is_file()]

    gh("release", "delete", tag, "--yes", "--cleanup-tag", check=False)
    gh("release", "create", tag, *[str(f) for f in files], "--title", f"Clips: {heading}"[:240], "--notes-file", str(out / "clips.md"))

    base = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo()}/releases"
    body = comment_body(manifest, f"{base}/tag/{tag}", f"{base}/download/{tag}")
    (args.work / "comment.md").write_text(body, encoding="utf-8")
    gh("issue", "comment", str(args.issue), "--body-file", str(args.work / "comment.md"))
    gh("issue", "edit", str(args.issue), "--add-label", "clipped", "--remove-label", "processing", "--remove-label", "failed", check=False)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")


def comment_body(manifest: dict, release_url: str, download_url: str) -> str:
    clips = manifest["clips"]
    lines = [
        f"### {len(clips)} clip{'s' if len(clips) != 1 else ''} ready",
        "",
        f"{package.picker_line(manifest)} [All files and the contact sheet]({release_url})",
    ]
    recording = manifest.get("recording")
    if recording:
        lines += ["", f"From a {clock(recording['length'])} stream. {recording['stretches']} stretches of it were looked at closely."]
    if manifest.get("note"):
        lines += ["", f"> **Note:** {manifest['note']}"]
    lines += ["", "| # | Headline | Length | Starts at | File |", "|---|---|---|---|---|"]
    for clip in clips:
        headline = clip["title"].replace("|", "\\|")
        at = clock(clip["source_start"])
        if clip.get("watch"):
            at = f"[{at}]({clip['watch']})"  # opens the stream at that moment
        lines.append(
            f"| {clip['rank']} | {headline} | {clock(clip['duration'])} | {at} "
            f"| [{clip['file']}]({download_url}/{clip['file']}) |"
        )
    lines += ["", "<details><summary>Post text and reasons</summary>", ""]
    for clip in clips:
        lines += [f"**{clip['rank']}. {clip['title']}**", "", clip["post"], ""]
        if clip.get("why"):
            lines += [f"_Why:_ {clip['why']}", ""]
    lines += ["</details>"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- fail


def cmd_fail(args) -> None:
    """Say what went wrong on the issue and mark it failed."""
    error_file = args.work / "error.txt"
    reason = error_file.read_text(encoding="utf-8").strip() if error_file.is_file() else ""
    if not reason:
        reason = "The run stopped before it could report a reason. The run log has the details."
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo()}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    body = (
        "### Could not make clips\n\n"
        f"{reason[:3000]}\n\n"
        "To try again, fix the problem (edit the issue if the link was wrong), then start the Clips workflow by "
        f"hand with this issue's number, or remove the `failed` label and the nightly run will take it. [Run log]({run_url})\n"
    )
    gh("issue", "comment", str(args.issue), "--body", body, check=False)
    gh("issue", "edit", str(args.issue), "--add-label", "failed", "--remove-label", "processing", check=False)


# ----------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="clipper.ci", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    for name in ("start", "publish", "fail"):
        command = sub.add_parser(name)
        command.add_argument("--issue", required=True, type=int)
        command.add_argument("--work", type=Path, default=Path("work"))
    args = parser.parse_args(argv)
    setup_logging()
    try:
        {"plan": cmd_plan, "start": cmd_start, "publish": cmd_publish, "fail": cmd_fail}[args.command](args)
    except ClipperError as exc:
        log.error("%s", exc)
        work = getattr(args, "work", None)
        if work is not None and args.command != "fail":
            work.mkdir(parents=True, exist_ok=True)
            (work / "error.txt").write_text(str(exc), encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
