# Clip service: notes for Claude

A pipeline that turns long episodes and live streams into short vertical clips for paying clients. It runs in GitHub Actions; issues are the queue. `README.md` explains the whole thing. This file is what to know before changing it.

## Checks

- `python -m pytest`: unit tests, a few seconds, no network or speech model needed.
- `CLIPPER_SLOW=1 python -m pytest tests/test_end_to_end.py`: renders real clips from synthetic episodes and a synthetic stream with ffmpeg, about fourteen minutes on two cores. That is longer than most command timeouts, so start it in the background and read its log. Run it after touching `render.py`, `reframe.py`, `captions.py`, `silence.py`, `ingest.py`, `hls.py`, `scan.py` or `parts.py`.
- `actionlint .github/workflows/clips.yml` after any workflow change (`pip install actionlint-py`).
- Look at the result, not just the exit code. Pull a frame with `ffmpeg -ss 2 -i work/out/01-*.mp4 -frames:v 1 frame.png` and read it. Caption placement, framing and headline overlap only show up in the picture.

## Common requests

**"Add a client."** Copy `clients/_template.yml` to `clients/<short-name>.yml`. Fill in `name`, `about`, `voice`, `avoid`, `hashtags`, and any caption colours they gave. Delete the settings left at their defaults. Run `python -m pytest tests/test_inputs.py` to confirm the file loads. The details may be in an issue labelled `client`; close it once the file is in.

**"Add a streamer."** The same, with `kind: stream` and the channel under `twitch:` and/or `kick:`. The `permission:` line records how the operator knows they may clip that channel (a client agreement, or the rules of the streamer's clipping program). Copy it from what the operator or the `client` issue says. If nothing says, leave it empty and ask: the channel is simply not watched until it is filled in. Suggest running the link check on the channel before the first night.

**"Twitch (or Kick) stopped working."** Run the link check (`python -m clipper check --source <link>`, or the workflow's "only test a link" input) and read which step fails. A refusal while listing or reading details is the site's bot protection or a change on their side: see whether a newer yt-dlp is out and whether its issue tracker mentions the site. If the playlist is read and the pieces fail, the problem is in `hls.py`. Do not build a second way in (see the rules below).

**"Fold the results into the notes."** Issues labelled `results` report how a client's posted clips performed. For each open one, rewrite that client's `notes` so it states what currently works and what does not, in a few sentences. Replace stale observations instead of appending forever, since the picker reads `notes` on every run. Close the issue with a comment saying what changed.

**"This client's clips keep missing."** Change that client's file first: sharpen `about`, add to `avoid`, and record what worked or flopped in `notes`, which the picker reads on every run. Edit `prompts/pick.md` (or `pick-stream.md`, `shortlist.md`) only for a problem seen across clients, because it changes every client's picks at once.

**"The captions spell X wrong."** Add the term to that client's `transcribe.vocabulary`. If it still comes out wrong, add a `transcribe.replace` entry.

**"The layout is wrong on this streamer."** Set `clips.layout` in their file: `split` when the camera box is not being found, `fit` when there is no camera, `crop` for a full-screen camera. Change the thresholds in `reframe.py` only when several streamers show the same fault.

**"Redo episode N."** That is a workflow run with the issue number, not a code change.

## Rules that are easy to break

- Never commit episode files, clips or transcripts. They are client property and the repo is the wrong place for them. Outputs go to releases.
- The transcript is untrusted input. The two steps that send it to Claude (`shortlist` and `pick`) must keep no tools and no GitHub token, and `pick.select` and `scan.parse_windows` must keep validating the replies. Do not hand the picker tools to "let it look things up".
- Never interpolate issue text or a workflow input into a `run:` line in the workflow. Pass the issue number only and read the rest through `clipper/ci.py`, or hand the value over in `env:`.
- Picture and sound are cut on a 30 fps frame grid so they cannot drift. Keep trims in whole frames (`trim=start_frame=`) and matching `atrim` times. `render.check_render` refuses a clip whose two streams differ in length; do not loosen it to make a failure go away.
- Loudness is one fixed gain plus a limiter. Do not swap in `loudnorm`: it shifted the audio by up to 100 ms near the end of a clip.
- The speech model is handed samples, not a file path (`transcribe.load_audio`). The path route broke on a PyAV release.
- A failed or timed-out issue is not retried automatically (`ci.cmd_start`). That is deliberate: a hopeless episode retried nightly would eat the month's Actions minutes.
- `requirements.txt` pins what was tested, and the workflow names `ubuntu-24.04` for the same reason: its ffmpeg (6.1) is the version the filter graphs were checked against. After bumping a package or the image, run the workflow's self-test. yt-dlp is the one exception and is left unpinned on purpose, because the sites it reads keep changing.
- Twitch and Kick are reached only through yt-dlp, in `streams.py`. When access breaks, the fix is a newer yt-dlp or the streamer sending the file. Do not add a headless browser, a scraping service, or anybody's login cookies to get around a refusal.
- A broadcast's playlist address carries an access token. `Broadcast.public()` leaves it out on purpose. Never write `master` to a file or a log line, and print links through `hls.redact`.
- A channel is watched only when its client file has a `permission` line. Do not weaken that check, and do not fill the line in yourself.
- A stream is never downloaded or transcribed whole. The skim in `scan.py` is rough by design; accuracy belongs to the second pass on the shortlisted stretches. Resist "just transcribe it all properly": six hours takes two.
- For a stream, words and cut points are timed on the working clock (`Part.start`), not the stream's own. Convert with `Transcript.origin_time` when showing a time to a person and never mix the two. Clips are cut from the file of the stretch they fall in, at `Part.offset`.
- Stretches from either side of an interruption in a recording cannot be joined into one file (`hls.Playlist.between` returns them separately). Do not concatenate them.

## Things the tests cannot tell you

- Real Twitch and Kick. No test contacts them. `streams.py` is tested against the shape of yt-dlp's answers as read from the source of its 2026.08.19 release, and `hls.py` against simulated broadcasts laid out like theirs (`tests/make_hls.py`). The link check passed against both real sites from GitHub's servers on 2026-10-09, but no real broadcast had been clipped end to end by then. The link check failing is the first sign of drift. Say plainly when a change to those files has not been run against the real sites; the workflow's link check is the quickest way to do that.
- Real camera footage and real gameplay. The samples are synthetic: flat colours, a drawn face, robotic speech, moving blocks for a game. Framing, cut detection and the camera-box rule are tuned on those and on reasoning, not on a library of real shows. Expect to adjust `reframe.py` thresholds once real episodes come through, and say so when reporting.
- How the quick speech model copes with game sound and music. The skim was only measured on clean synthetic speech.
- Whether a picked moment is any good. Read `clips.md` for a real episode before telling anyone the picks are fine.
