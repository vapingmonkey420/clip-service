"""Long recordings: skim the whole thing, then look closely at only the best stretches.

Three steps, one per pipeline stage:

  skim       get the recording's sound, make a rough transcript and a loudness curve   -> scan.json
  choose     shortlist the stretches worth a close look                                -> windows.json
  gather     fetch those stretches at full quality, lay their sound end to end on one
             working clock, and transcribe that properly                               -> transcript.json

For a Twitch or Kick link the full-quality picture is fetched only for the
shortlisted stretches. Nothing else of the stream ever touches the disk.
"""

from __future__ import annotations

import wave
from pathlib import Path

from . import hls, ingest, scan, silence, streams
from .transcript import Part, Transcript, Word
from .util import ClipperError, clock, log, need, read_json, run, write_json

RATE = 16000
GAP = 1.0  # silence put between stretches on the working clock, so nothing runs on from one into the next
MIN_PART = 12.0  # a fetched stretch shorter than this cannot hold a clip


def is_long(episode: dict, cfg: dict) -> bool:
    return streams.is_long(episode.get("source", ""), cfg)


# ----------------------------------------------------------------------- skim


def skim(episode: dict, cfg: dict, work: Path) -> dict:
    need("ffmpeg")
    settings = cfg["stream"]
    limit = float(settings["max_hours"]) * 3600
    source = episode["source"]
    hints: list[dict] = []

    if streams.is_stream_link(source):
        broadcast = streams.resolve(source)
        session, levels = streams.open_playlists(broadcast)
        playlist = hls.load_media(session, hls.scan_variant(levels))
        if broadcast.live or not playlist.ended:
            raise ClipperError("That stream is still live. Run this again once it has ended.")
        picture = hls.best_variant(levels, int(settings["max_height"]))
        if playlist.duration < ingest.MIN_SECONDS:
            raise ClipperError(f"That broadcast is only {playlist.duration:.0f} seconds long.")
        site = {"twitch": "Twitch", "kick": "Kick"}.get(broadcast.platform, "Stream")
        log.info("%s broadcast, %s long. Fetching its sound.", site, clock(playlist.duration))
        wav = work / "scan.wav"
        length = fetch_sound(session, playlist, wav, limit, work / "scan")
        meta = {
            **broadcast.public(),
            "source": "hls",
            "duration": length,
            "full_duration": playlist.duration,
            "picture": f"{picture.name or picture.height} ({picture.width}x{picture.height})",
        }
        if settings["viewer_clips"] and broadcast.channel and broadcast.platform in ("twitch", "kick"):
            made = streams.viewer_clips(broadcast.platform, broadcast.channel)
            hints = streams.clip_hints(made, broadcast.started, length)
        if not episode.get("title") and broadcast.title:
            episode["title"] = broadcast.title
            write_json(work / "episode.json", episode)
    else:
        info = ingest.prepare(source, work, max_seconds=24 * 3600)
        wav = Path(info["audio"])
        length = min(float(info["duration"]), limit)
        meta = {"platform": "file", "id": "", "url": "", "title": "", "channel": "", "games": [], "source": "file",
                "duration": length, "full_duration": float(info["duration"])}  # fmt: skip

    per_second = scan.loudness(wav)[: int(length) + 1]
    supplied = episode.get("transcript")
    if supplied:
        lines = [line for line in scan.lines_from_transcript(Transcript.load(supplied)) if line.start < length]
        log.info("Using the supplied transcript for the skim: %d phrases", len(lines))
    else:
        lines = scan.rough_transcript(wav, settings, cfg["transcribe"].get("language") or "en", per_second, max_seconds=length)
    data = {
        "meta": meta,
        "lines": [[round(line.start, 2), round(line.end, 2), line.text] for line in lines],
        "loud": scan.loud_moments(per_second),
        "hints": hints,
    }
    write_json(work / "scan.json", data)
    if meta["source"] == "hls":
        wav.unlink(missing_ok=True)  # the skim is all that was wanted from it
    if meta["full_duration"] > length + 60:
        log.warning("Only the first %s of this %s recording was skimmed (stream.max_hours).", clock(length), clock(meta["full_duration"]))
    log.info("Skim done: %d phrases, %d loud moments, %d viewer clips", len(lines), len(data["loud"]), len(hints))
    return data


def fetch_sound(session, playlist: hls.Playlist, target: Path, limit: float, scratch: Path) -> float:
    """Download the cheapest version of a broadcast and write its sound as one WAV on the stream's clock.

    Each uninterrupted stretch of the recording is fetched and decoded by itself
    and placed at its own start time, so an interruption three hours in cannot
    shift everything after it. Returns the length written, in seconds.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    written = 0
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        for number in sorted({piece.run for piece in playlist.pieces}):
            pieces = [p for p in playlist.pieces if p.run == number and p.start < limit]
            if not pieces or pieces[-1].end - pieces[0].start < 5:
                continue  # nothing, or a stub such as the logo Kick tacks on the end
            media = hls.fetch(session, pieces, scratch / f"run{number}")
            decoded = scratch / f"run{number}.wav"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", media, "-vn", "-sn", "-map", "0:a:0", "-ac", "1",
                 "-af", f"aresample={RATE}:async=1:first_pts=0", "-c:a", "pcm_s16le", decoded], timeout=3 * 3600)  # fmt: skip
            media.unlink(missing_ok=True)
            begin = int(round(pieces[0].start * RATE))
            if begin > written:
                _write_silence(out, begin - written)
                written = begin
            # Exactly as long as the playlist says this stretch is, so the next one lands in the right place.
            want = max(0, int(round(pieces[-1].end * RATE)) - written)
            with wave.open(str(decoded), "rb") as part:
                left = min(want, part.getnframes())
                while left > 0:
                    block = part.readframes(min(left, RATE * 60))
                    if not block:
                        break
                    out.writeframes(block)
                    left -= len(block) // 2
                    written += len(block) // 2
            decoded.unlink(missing_ok=True)
            short = int(round(pieces[-1].end * RATE)) - written
            if short > 0:
                _write_silence(out, short)
                written += short
    if written < RATE * 5:
        raise ClipperError("No sound could be read from that broadcast.")
    return written / RATE


def _write_silence(out, frames: int) -> None:
    block = bytes(2 * RATE * 10)
    while frames > 0:
        take = min(frames, RATE * 10)
        out.writeframes(block[: take * 2])
        frames -= take


# --------------------------------------------------------------------- choose


def choose(episode: dict, cfg: dict, work: Path, mode: str = "auto") -> dict:
    data = read_json(work / "scan.json")
    lines = [scan.Line(i, float(a), float(b), str(text)) for i, (a, b, text) in enumerate(data["lines"])]
    meta = {**data["meta"], "notes": episode.get("notes", ""), "title": episode.get("title") or data["meta"].get("title", "")}
    result = scan.shortlist(lines, data["loud"], data["hints"], cfg, meta, mode)
    write_json(work / "windows.json", result)
    minutes = sum(w["end"] - w["start"] for w in result["windows"]) / 60
    log.info("Shortlisted %d stretches (%.0f minutes in all) with %s", len(result["windows"]), minutes, result["picker"])
    if result["note"]:
        log.warning(result["note"])
    return result


# --------------------------------------------------------------------- gather


def gather(episode: dict, cfg: dict, work: Path) -> Transcript:
    data = read_json(work / "scan.json")
    windows = read_json(work / "windows.json")["windows"]
    found = _fetch_stretches(episode, cfg, work, windows) if data["meta"]["source"] == "hls" else _file_stretches(work, windows)
    if not found:
        raise ClipperError("None of the shortlisted stretches could be fetched, so there is nothing to clip.")

    # Lay the stretches' sound end to end. Every word and every cut is then timed on this one working clock.
    stitched = work / "stitched.wav"
    parts: list[Part] = []
    cursor = 0
    with wave.open(str(stitched), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        for n, item in enumerate(found):
            frames = int(round(item["length"] * RATE))
            out.writeframes(_read_frames(item["info"]["audio"], int(round(item["offset"] * RATE)), frames))
            parts.append(Part(n, cursor / RATE, (cursor + frames) / RATE, item["origin"], item["info"]["path"], item["offset"], item["why"]))
            cursor += frames
            if n < len(found) - 1:
                _write_silence(out, int(GAP * RATE))
                cursor += int(GAP * RATE)
    total = cursor / RATE

    supplied = episode.get("transcript")
    if supplied:
        given = Transcript.load(supplied).words
        words = [
            Word(w.text, part.start + (w.start - part.origin), part.start + (w.end - part.origin))
            for part in parts
            for w in given
            if part.origin <= w.start and w.end <= part.origin + (part.end - part.start)
        ]
        transcript = Transcript(words=words, duration=total, model="supplied", parts=parts)
        log.info("Using the supplied transcript: %d words in the shortlisted stretches", len(words))
    else:
        from .transcribe import transcribe

        transcript = transcribe(stitched, cfg["transcribe"])
        # The speech model sometimes hears a word in the silence between two stretches. Drop those.
        transcript.words = [w for w in transcript.words if any(p.start <= w.start and w.end <= p.end + 0.05 for p in parts)]
        transcript.parts, transcript.duration = parts, total
    quiet = silence.find_silences(stitched)
    transcript.set_pauses(silence.pauses_after_words(transcript.words, quiet))
    transcript.save(work / "transcript.json")
    media = {item["info"]["path"]: item["info"] for item in found}
    write_json(work / "probe.json", {"long": True, "audio": str(stitched), "duration": total, "media": media})
    log.info("Looked closely at %d stretches (%s in all): %d sentences", len(parts), clock(total), len(transcript.sentences))
    return transcript


def _fetch_stretches(episode: dict, cfg: dict, work: Path, windows: list[dict]) -> list[dict]:
    """Download each shortlisted stretch of a broadcast at full quality."""
    # Asked for again here, not carried over from the skim: playlist addresses carry access tokens that expire.
    broadcast = streams.resolve(episode["source"])
    session, levels = streams.open_playlists(broadcast)
    playlist = hls.load_media(session, hls.best_variant(levels, int(cfg["stream"]["max_height"])))
    folder = work / "parts"
    folder.mkdir(parents=True, exist_ok=True)
    found = []
    number = 0
    for window in windows:
        # One download per uninterrupted stretch: a window that straddles an interruption becomes two.
        for pieces in playlist.between(window["start"] - 1.0, window["end"] + 1.0):
            if pieces[-1].end - pieces[0].start < MIN_PART:
                continue
            number += 1
            try:
                raw = hls.fetch(session, pieces, folder / f"{number:02d}-raw")
                info = ingest.describe(raw, folder, stem=f"{number:02d}", min_seconds=5, max_seconds=3600, rewrap=True)
            except ClipperError as exc:
                log.warning("The stretch at %s could not be fetched and was skipped: %s", clock(window["start"]), str(exc).splitlines()[0][:200])
                continue
            if Path(info["path"]) != raw:
                raw.unlink(missing_ok=True)  # keep only the copy that clips are cut from
            begin = pieces[0].start
            a, b = max(window["start"], begin), min(window["end"], begin + info["duration"])
            if b - a >= MIN_PART:
                found.append({"info": info, "offset": a - begin, "origin": a, "length": b - a, "why": window.get("why", "")})
    size = sum(Path(item["info"]["path"]).stat().st_size for item in found) / 1024**2
    log.info("Fetched %d stretches at full quality (%.0f MB) for the %d shortlisted", len(found), size, len(windows))
    return found


def _file_stretches(work: Path, windows: list[dict]) -> list[dict]:
    """For a recording that is already a file on disk, the stretches are just places in it."""
    info = read_json(work / "probe.json")
    if info.get("long"):
        info = next(iter(info["media"].values()))  # a second run in the same work folder
    return [
        {"info": info, "offset": w["start"], "origin": w["start"], "length": w["end"] - w["start"], "why": w.get("why", "")}
        for w in windows
        if w["end"] - w["start"] >= MIN_PART
    ]


def _read_frames(wav_path, start: int, count: int) -> bytes:
    """`count` samples of a 16 kHz mono WAV from `start`, padded with silence if the file runs out."""
    with wave.open(str(wav_path), "rb") as fh:
        if (fh.getnchannels(), fh.getsampwidth(), fh.getframerate()) != (1, 2, RATE):
            raise ClipperError(f"{wav_path} is not 16 kHz mono 16-bit audio.")
        fh.setpos(min(max(start, 0), fh.getnframes()))
        data = fh.readframes(count)
    return data + bytes(2 * count - len(data))


# ---------------------------------------------------------------------- check


def check_link(source: str, workdir: Path) -> tuple[bool, list[str]]:
    """Try every step of reaching a Twitch or Kick link, without clipping anything.

    Returns (everything worked, report lines). This exists because access to both
    sites depends on things outside this code: their bot protection, the address
    the request comes from, and the yt-dlp version.
    """
    report: list[str] = []
    found = streams.classify(source)
    if not found:
        return False, ["That is not a Twitch or Kick link. Use a channel link or a link to one past broadcast."]
    platform, kind, key = found

    def step(label: str, action):
        try:
            value = action()
        except ClipperError as exc:
            report.append(f"- FAILED: {label}. {str(exc)[:400]}")
            return None
        except Exception as exc:  # a check should report, never crash
            report.append(f"- FAILED: {label}. {type(exc).__name__}: {str(exc)[:300]}")
            return None
        return value

    if kind == "channel":
        listing = step("list the channel's past broadcasts", lambda: streams.recent(platform, key, limit=5))
        if listing is None:
            return False, report
        report.append(f"- OK: {platform.title()} lists {len(listing)} recent past broadcast{'s' if len(listing) != 1 else ''} for `{key}`.")
        for item in listing:
            state = " (live now)" if item["live"] else ""
            length = f", {clock(item['duration'])}" if item["duration"] else ""
            report.append(f"  - {item['title'] or 'Untitled'}{length}{state}: {item['url']}")
        if not any(not item["live"] for item in listing):
            report.append("- Nothing finished to test further. On Twitch, the streamer has to switch on 'Store past broadcasts'.")
            return False, report

    # For a channel this takes its latest finished broadcast, skipping one still being recorded, exactly as clipping would.
    broadcast = step("read the broadcast's details", lambda: streams.resolve(source))
    if broadcast is None:
        return False, report
    games = ", ".join(name for _, name in broadcast.games[:4])
    report.append(
        f"- OK: details read. \"{broadcast.title or 'Untitled'}\" on {broadcast.channel or 'unknown channel'}"
        + (f", {clock(broadcast.duration)}" if broadcast.duration else "")
        + (f", {games}" if games else "")
        + (" (still live)" if broadcast.live else "")
    )
    opened = step("read the broadcast's playlist", lambda: streams.open_playlists(broadcast))
    if opened is None:
        return False, report
    session, levels = opened
    report.append("- OK: quality levels: " + ", ".join(f"{v.name or v.height} ({'sound only' if v.audio_only else f'{v.width}x{v.height} {v.video_codec}'})" for v in levels))
    sound, picture = hls.scan_variant(levels), step("find a picture to cut clips from", lambda: hls.best_variant(levels))
    if picture is None:
        return False, report
    lists = step("read the lists of media files", lambda: (hls.load_media(session, sound), hls.load_media(session, picture)))
    if lists is None:
        return False, report
    sound_list, picture_list = lists
    report.append(
        f"- OK: {len(picture_list.pieces)} media files covering {clock(picture_list.duration)}, in {len(picture_list.runs())} "
        f"uninterrupted stretch{'es' if len(picture_list.runs()) != 1 else ''}; " + ("finished." if picture_list.ended else "still being recorded.")
    )
    middle = picture_list.pieces[len(picture_list.pieces) // 2]
    fetched = step("download media", lambda: (
        hls.fetch(session, sound_list.pieces[:1], workdir / "check-sound"),
        hls.fetch(session, [middle], workdir / "check-picture"),
    ))  # fmt: skip
    if fetched is None:
        return False, report
    details = step("read the downloaded picture", lambda: ingest.probe(fetched[1]))
    for path in fetched:
        path.unlink(missing_ok=True)
    if details is None or not details["has_video"]:
        report.append("- FAILED: the downloaded piece holds no picture.")
        return False, report
    report.append(f"- OK: downloaded a piece of sound and a piece of picture ({details['width']}x{details['height']}, {details['fps']:.0f} fps).")
    if broadcast.channel and broadcast.platform in ("twitch", "kick"):
        made = streams.viewer_clips(broadcast.platform, broadcast.channel)
        placed = streams.clip_hints(made, broadcast.started, picture_list.duration)
        report.append(f"- Viewer clips: {len(made)} found for the channel this week, {len(placed)} of them from this broadcast. (These are only hints.)")
    return True, report

