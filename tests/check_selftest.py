"""Checks the output of the workflow's self-test and prints a short report.

    python tests/check_selftest.py work                         # the sample episode
    python tests/check_selftest.py work/stream --sample stream  # the sample stream

Fails (exit 1) only when the clips themselves are wrong. How closely the speech
model heard the sample script is reported, since robotic test speech is harder
for it than a real voice.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_sample import SCRIPT, STREAM_SCRIPT  # noqa: E402

from clipper.util import actions_notice  # noqa: E402  (make_sample put the repo on the path)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("work", nargs="?", default="work", type=Path)
    parser.add_argument("--sample", choices=("episode", "stream"), default="episode")
    args = parser.parse_args()
    work, stream = args.work, args.sample == "stream"
    label = "Self-test: sample stream" if stream else "Self-test: sample episode"

    problems = []
    manifest_path = work / "out" / "manifest.json"
    if not manifest_path.is_file():
        print(f"FAIL ({label}): no manifest.json; the pipeline did not finish.")
        return 1
    manifest = json.loads(manifest_path.read_text())
    if not manifest["clips"]:
        problems.append("no clips were produced")
    for clip in manifest["clips"]:
        path = work / "out" / clip["file"]
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration", "-of", "json", str(path)],
            capture_output=True, text=True,
        )  # fmt: skip
        streams = {s["codec_type"]: s for s in json.loads(probe.stdout or "{}").get("streams", [])}
        if set(streams) != {"video", "audio"}:
            problems.append(f"{clip['file']}: expected one video and one audio stream")
            continue
        if (streams["video"]["width"], streams["video"]["height"]) != (1080, 1920):
            problems.append(f"{clip['file']}: not 1080x1920")
        if abs(float(streams["video"]["duration"]) - float(streams["audio"]["duration"])) > 0.05:
            problems.append(f"{clip['file']}: picture and sound differ in length")
    if stream and not manifest.get("recording"):
        problems.append("the stream was not handled as a stream")
    if stream and manifest["clips"] and not any("split" in clip["layout"] for clip in manifest["clips"]):
        problems.append("no clip used the camera-over-gameplay layout, though the sample has a camera box")

    transcript = json.loads((work / "transcript.json").read_text())
    heard = words(" ".join(w[0] for w in transcript["words"]))
    said = words(" ".join(row[1] for row in (STREAM_SCRIPT if stream else SCRIPT)))
    matcher = difflib.SequenceMatcher(None, said, heard, autojunk=False)
    # For a stream only the shortlisted stretches are transcribed, so count how much of what was heard is right.
    match = sum(block.size for block in matcher.get_matching_blocks()) / max(len(heard), 1) if stream else matcher.ratio()

    report = [
        f"Clips:      {len(manifest['clips'])} ({', '.join(c['layout'] for c in manifest['clips'])})",
        f"Picked by:  {manifest['picker']}" + (f" ({manifest['model']})" if manifest["model"] else ""),
    ]
    if stream and manifest.get("recording"):
        report.append(f"Stream:     {manifest['recording']['stretches']} stretches shortlisted from {manifest['recording']['length'] / 60:.1f} minutes")
    if manifest.get("note"):
        report.append(f"Note:       {manifest['note']}")
    report.append(f"Transcript: {len(heard)} words heard, {match:.0%} match with the sample script ({transcript.get('model')})")
    if match < 0.6:
        report.append("WARNING: the transcript is a poor match. The sample voice is synthetic, so judge on a real episode.")
    report += [f"FAIL: {problem}" for problem in problems]
    if not problems:
        report.append("OK: download the 'selftest-clips' artifact from this run to watch the result.")
    print("\n".join([label, *report]))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:  # show the same report on the run's summary page
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### {label}\n\n```\n" + "\n".join(report) + "\n```\n")
    actions_notice(label, "\n".join(report))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
