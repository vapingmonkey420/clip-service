"""Turn a video file into a folder shaped like a past broadcast on Twitch or Kick.

The platforms serve a stream as a master playlist naming a few quality levels,
each a playlist of short media files. This writes the same structure from any
local video, so the stream-fetching code can be tested by serving the folder
with `python -m http.server` and pointing the pipeline at master.m3u8.

    python tests/make_hls.py work/stream.mp4 work/vod
    python tests/make_hls.py work/stream.mp4 work/vod --style kick     # no sound-only level; a logo tacked on the end
    python tests/make_hls.py work/stream.mp4 work/vod --muted 12-13    # pieces Twitch has muted
    python tests/make_hls.py work/stream.mp4 work/vod --break-at 200   # the recording was interrupted there
    python tests/make_hls.py work/stream.mp4 work/vod --live           # still being recorded
    python tests/make_hls.py work/stream.mp4 work/vod --fmp4           # fragmented MP4 pieces behind a header file,
                                                                       # as Twitch serves its newer HEVC and AV1 streams
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


def ffmpeg(*args: str) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{proc.stderr[-800:]}")


def package(source: Path, folder: Path, codec_args: list[str], seconds: int, offset: float, fmp4: bool = False) -> list[tuple[float, str]]:
    """Split `source` into pieces in `folder`. Returns [(length, file name), ...]."""
    folder.mkdir(parents=True, exist_ok=True)
    layout = ["-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init-0.mp4", "-hls_segment_filename", str(folder / "%d.mp4")] if fmp4 \
        else ["-hls_segment_filename", str(folder / "%d.ts")]  # fmt: skip
    ffmpeg(
        "-i", str(source), *codec_args,
        # Real broadcasts carry the time since the stream began, not zero, in every piece.
        "-output_ts_offset", f"{offset:.3f}", "-muxdelay", "0",
        "-f", "hls", "-hls_time", str(seconds), "-hls_list_size", "0", "-hls_playlist_type", "vod",
        *layout, str(folder / "ffmpeg.m3u8"),
    )  # fmt: skip
    listing = (folder / "ffmpeg.m3u8").read_text()
    (folder / "ffmpeg.m3u8").unlink()
    return [(float(length), name) for length, name in re.findall(r"#EXTINF:([0-9.]+),\s*\n(\S+)", listing)]


def write_playlist(folder: Path, pieces, *, muted: set[int], break_at: float | None, live: bool, target: int, logo: bool,
                   fmp4: bool = False) -> None:  # fmt: skip
    lines = ["#EXTM3U", f"#EXT-X-VERSION:{6 if fmp4 else 3}", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-PLAYLIST-TYPE:EVENT",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-TWITCH-ELAPSED-SECS:0.000",
             f"#EXT-X-TWITCH-TOTAL-SECS:{sum(length for length, _ in pieces):.3f}"]  # fmt: skip
    if fmp4:
        lines.append('#EXT-X-MAP:URI="init-0.mp4"')
    clock = 0.0
    for index, (length, name) in enumerate(pieces):
        if break_at is not None and clock <= break_at < clock + length and index:
            lines.append("#EXT-X-DISCONTINUITY")
        if index in muted:
            # Twitch lists a muted stretch under a name the public cannot fetch; the muted copy sits beside it.
            stem, dot, ext = name.rpartition(".")
            (folder / name).rename(folder / f"{stem}-muted.{ext}")
            name = f"{stem}-unmuted.{ext}"
        lines += [f"#EXTINF:{length:.3f},", name]
        clock += length
    if logo and pieces:
        # Kick appends its logo as a separate, unrelated piece.
        logo_name = "logo." + pieces[0][1].rpartition(".")[2]
        (folder / logo_name).write_bytes((folder / pieces[0][1]).read_bytes())
        lines += ["#EXT-X-DISCONTINUITY", f"#EXTINF:{pieces[0][0]:.3f},", logo_name]
    if not live:
        lines.append("#EXT-X-ENDLIST")
    (folder / "index-dvr.m3u8").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--style", choices=("twitch", "kick"), default="twitch")
    parser.add_argument("--muted", default="", help="piece numbers to mark as muted, e.g. 12-13")
    parser.add_argument("--break-at", type=float, default=None, help="seconds into the stream where the recording was interrupted")
    parser.add_argument("--live", action="store_true", help="leave the playlists open, as while a stream is running")
    parser.add_argument("--offset", type=float, default=0.0, help="clock time carried by the first piece, in seconds")
    parser.add_argument("--fmp4", action="store_true", help="fragmented MP4 pieces behind a header file, not MPEG-TS")
    args = parser.parse_args()

    muted: set[int] = set()
    if args.muted:
        first, _, last = args.muted.partition("-")
        muted = set(range(int(first), int(last or first) + 1))
    seconds = 10 if args.style == "twitch" else 4
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(args.source)], capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")  # fmt: skip
    width, height, rate = int(probe[0]), int(probe[1]), probe[2]
    top, _, bottom = rate.partition("/")
    fps = int(top) / int(bottom) if top.isdigit() and bottom.isdigit() and int(bottom) else 30.0

    levels = [("chunked", f"{height}p{round(fps)}", ["-c", "copy"], f'CODECS="avc1.64002A,mp4a.40.2",RESOLUTION={width}x{height}', 6_000_000)]
    small = ["-vf", "scale=-2:160", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-g", "60", "-keyint_min", "60",
             "-sc_threshold", "0", "-c:a", "copy"]  # fmt: skip
    levels.append(("160p30", "160p", small, f'CODECS="avc1.4D400C,mp4a.40.2",RESOLUTION={round(width * 160 / height / 2) * 2}x160', 300_000))
    if args.style == "twitch":
        levels.append(("audio_only", "Audio Only", ["-vn", "-c:a", "copy"], 'CODECS="mp4a.40.2"', 160_000))

    master = ["#EXTM3U"]
    if args.style == "twitch":
        master.append('#EXT-X-TWITCH-INFO:ORIGIN="s3",B="false",REGION="NA",CLUSTER="cloudfront_vod",MANIFEST-CLUSTER="cloudfront_vod"')
    for folder, name, codec_args, attributes, bandwidth in levels:
        pieces = package(args.source, args.out / folder, codec_args, seconds, args.offset, args.fmp4)
        write_playlist(args.out / folder, pieces, muted=muted, break_at=args.break_at, live=args.live, target=seconds,
                       logo=args.style == "kick", fmp4=args.fmp4)  # fmt: skip
        if args.style == "twitch":
            master.append(f'#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="{folder}",NAME="{name}",AUTOSELECT=YES,DEFAULT=YES')
            rate = "" if folder == "audio_only" else f",FRAME-RATE={fps:.3f}"
            master.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},{attributes},VIDEO="{folder}"{rate}')
        else:
            master.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},{attributes},FRAME-RATE={fps:.3f}")
        master.append(f"{folder}/index-dvr.m3u8")
    (args.out / "master.m3u8").write_text("\n".join(master) + "\n")
    print(f"Wrote {args.out}/master.m3u8 with {len(levels)} quality levels of {len(pieces)} pieces each ({args.style} style)")


if __name__ == "__main__":
    main()
