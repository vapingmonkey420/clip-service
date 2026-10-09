"""Builds synthetic episodes and a synthetic stream and runs the real pipeline on them with ffmpeg.

Takes about fourteen minutes on two cores, so it only runs when asked:

    CLIPPER_SLOW=1 python -m pytest tests/test_end_to_end.py

It uses the transcripts that come with the samples, so no speech model is needed.
The workflow's self-test covers the same ground with real speech recognition.
The stream is served to the pipeline from this machine, in the layout Twitch and
Kick use, so everything about clipping a stream is exercised except reaching
those two sites.
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from clipper import cli

TESTS = Path(__file__).resolve().parent

pytestmark = pytest.mark.skipif(
    os.environ.get("CLIPPER_SLOW") != "1" or not shutil.which("ffmpeg"),
    reason="slow; set CLIPPER_SLOW=1 (needs ffmpeg with flite)",
)


def streams(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,duration,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


@pytest.mark.parametrize(
    "mode,suffix,layout",
    [
        ("wide", ".mp4", "stack"),  # two hosts side by side: one above the other
        ("switched", ".mp4", "crop"),  # a camera per host: follow whoever is on screen
        ("screen", ".mp4", "fit"),  # nobody in shot: show the whole picture
        ("audio", ".mp3", "audiogram"),
    ],
)
def test_sample_episode_becomes_clips(tmp_path, mode, suffix, layout):
    sample = tmp_path / f"sample{suffix}"
    subprocess.run([sys.executable, str(TESTS / "make_sample.py"), str(sample), "--mode", mode], check=True)
    work = tmp_path / "work"
    code = cli.main([
        "run", "--client", "demo", "--source", str(sample), "--transcript", str(sample.with_suffix(".transcript.json")),
        "--title", "Sample", "--work", str(work), "--picker", "heuristic",
    ])  # fmt: skip
    assert code == 0, (work / "error.txt").read_text() if (work / "error.txt").exists() else "no error file"

    manifest = json.loads((work / "out" / "manifest.json").read_text())
    assert manifest["picker"] == "heuristic" and len(manifest["clips"]) == 2
    assert (work / "out" / "clips.md").is_file() and (work / "out" / "contact-sheet.jpg").is_file()
    assert not (work / "tmp").exists()
    for clip in manifest["clips"]:
        info = streams(work / "out" / clip["file"])
        video, audio = info["video"], info["audio"]
        assert (video["width"], video["height"], video["codec_name"]) == (1080, 1920, "h264")
        assert video["r_frame_rate"] == "30/1" and audio["codec_name"] == "aac"
        # Picture and sound the same length, and the length the manifest says.
        assert abs(float(video["duration"]) - float(audio["duration"])) < 0.05
        assert abs(float(video["duration"]) - clip["duration"]) < 0.05
        assert 8 * 0.8 <= clip["duration"] <= 30 * 1.1
        assert clip["layout"] == layout
        assert clip["post"].endswith("#podcasting") and clip["text"]


def test_a_bad_link_fails_with_a_reason_on_file(tmp_path):
    work = tmp_path / "work"
    code = cli.main(["run", "--client", "demo", "--source", str(tmp_path / "missing.mp4"), "--work", str(work)])
    assert code == 1
    assert "Getting the episode failed" in (work / "error.txt").read_text()


# --------------------------------------------------------------------- streams


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    """A five-minute pretend gaming stream, as a file."""
    folder = tmp_path_factory.mktemp("stream")
    subprocess.run([sys.executable, str(TESTS / "make_sample.py"), str(folder / "stream.mp4"), "--mode", "stream"], check=True)
    return folder


@pytest.fixture(scope="module")
def served(stream):
    """Serves the stream folder over HTTP on this machine. Yields a function: served("vod") -> the master playlist link."""

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(stream)))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def link(name: str, *options: str) -> str:
        if not (stream / name).is_dir():
            subprocess.run([sys.executable, str(TESTS / "make_hls.py"), str(stream / "stream.mp4"), str(stream / name),
                            "--offset", "7200", *options], check=True)  # fmt: skip
        return f"http://127.0.0.1:{server.server_address[1]}/{name}/master.m3u8"

    yield link
    server.shutdown()


def clip_stream(source: str, stream: Path, work: Path) -> int:
    return cli.main([
        "run", "--client", "demo-stream", "--source", source, "--transcript", str(stream / "stream.transcript.json"),
        "--title", "Sample stream", "--work", str(work), "--picker", "heuristic",
    ])  # fmt: skip


def sound_position(clip: Path, original: Path) -> float:
    """Where in `original` the first four seconds of `clip` are heard, in seconds."""
    import numpy as np

    def samples(path):
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                              "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout  # fmt: skip
        return np.frombuffer(raw, np.int16).astype(np.float32)

    whole, probe = samples(original), samples(clip)[: 4 * 8000]
    probe = probe - probe.mean()
    size = 1 << (len(whole) + len(probe) - 1).bit_length()
    match = np.fft.irfft(np.fft.rfft(whole, size) * np.conj(np.fft.rfft(probe, size)), size)[: len(whole)]
    return int(np.argmax(match)) / 8000


def check_stream_clips(work: Path, stream: Path) -> dict:
    manifest = json.loads((work / "out" / "manifest.json").read_text())
    assert manifest["recording"]["stretches"] >= 1 and len(manifest["clips"]) == 3
    assert "From a 5:" in (work / "out" / "clips.md").read_text()
    for clip in manifest["clips"]:
        info = streams(work / "out" / clip["file"])
        assert (info["video"]["width"], info["video"]["height"]) == (1080, 1920)
        assert abs(float(info["video"]["duration"]) - float(info["audio"]["duration"])) < 0.05
        assert 8 * 0.8 <= clip["duration"] <= 30 * 1.1
        # The clip really is the stretch of the stream the manifest says it is.
        assert abs(sound_position(work / "out" / clip["file"], stream / "stream.mp4") - clip["source_start"]) < 0.06
    return manifest


def test_a_stream_file_is_skimmed_shortlisted_and_clipped(stream, tmp_path):
    work = tmp_path / "work"
    assert clip_stream(str(stream / "stream.mp4"), stream, work) == 0, (work / "error.txt").read_text() if (work / "error.txt").exists() else ""
    manifest = check_stream_clips(work, stream)
    # The sample has a camera box over gameplay for its first 4:36 and the camera full-screen after that.
    for clip in manifest["clips"]:
        assert clip["layout"] == ("split" if clip["source_end"] < 276 else "fit")
    assert json.loads((work / "scan.json").read_text())["loud"], "the shouted line should register as a loud moment"


@pytest.mark.parametrize(
    "name,options",
    [
        ("twitch", ()),  # three quality levels, one of them sound only
        ("kick", ("--style", "kick")),  # no sound-only level, and a logo tacked on after an interruption
        ("rough", ("--break-at", "200", "--muted", "12-13")),  # an interrupted recording with muted pieces
        ("fmp4", ("--fmp4", "--break-at", "200")),  # the newer layout: fragments behind a header file
    ],
)
def test_a_stream_is_clipped_from_its_playlist(stream, served, tmp_path, name, options):
    work = tmp_path / "work"
    assert clip_stream(served(name, *options), stream, work) == 0, (work / "error.txt").read_text() if (work / "error.txt").exists() else ""
    manifest = check_stream_clips(work, stream)
    assert all(clip["layout"] in ("split", "fit") for clip in manifest["clips"])
    # Only the shortlisted stretches were fetched at full quality, and the skimmed sound was thrown away.
    assert sorted(p.suffix for p in (work / "parts").iterdir()).count(".mkv") == len(json.loads((work / "probe.json").read_text())["media"])
    assert not (work / "scan.wav").exists()
    if name in ("rough", "fmp4"):
        # The stretch that straddled the interruption came down as two, so the moment after it was not lost.
        assert any(200 < clip["source_start"] < 230 for clip in manifest["clips"])


def test_a_stream_that_is_still_live_is_refused(stream, served, tmp_path):
    work = tmp_path / "work"
    assert clip_stream(served("live", "--live"), stream, work) == 1
    assert "still live" in (work / "error.txt").read_text()


def test_the_link_check_reports_what_it_found(stream, served, tmp_path, capsys):
    assert cli.main(["check", "--source", served("twitch"), "--work", str(tmp_path / "check")]) == 0
    report = capsys.readouterr().out
    assert "everything worked" in report and "sound only" in report and "1280x720" in report and "5:3" in report
    assert cli.main(["check", "--source", "https://www.dropbox.com/s/abc/ep.mp4", "--work", str(tmp_path / "check")]) == 1

