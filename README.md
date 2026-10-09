# Clip service

Turns a client's long episode or live stream into short vertical clips with burned-in captions, on GitHub's servers, with nobody at a keyboard.

You open an issue with a link to the episode, or to a past broadcast on Twitch or Kick. A run fetches it, transcribes it, has Claude pick the moments, cuts and captions each one, and replies on the issue with the clips, post text for each, and a contact sheet. Every night it also sweeps up anything still waiting, and files new episodes and broadcasts from the feeds and channels it watches.

| Who | Does what |
|---|---|
| **GitHub Actions** | Runs the pipeline. Issues are the queue; labels are the state. |
| **Claude** | Picks the moments and writes the headline and post text. In a Claude Code session, also maintains this repo (see `CLAUDE.md`). |
| **You, or an agent such as Meta Muse** | Finds clients, files episode requests, reviews, delivers, invoices (see `muse/prompts.md`). |

## Set it up

1. **Create a private repository** on GitHub and put these files in it. Private matters: the clips are attached to the repo's releases, and on a public repo anyone could download a client's footage.
2. **Give the picker a Claude credential.** In the repo: Settings → Secrets and variables → Actions → New repository secret. Add one of:
   - `ANTHROPIC_API_KEY`: a key from the [Claude Console](https://platform.claude.com). Billed per use.
   - `CLAUDE_CODE_OAUTH_TOKEN`: run `claude setup-token` on a computer with Claude Code installed and paste what it prints. Uses your Claude subscription.

   Without either, the pipeline still runs with a rule-based picker and says so on the issue. The clips are usable but noticeably less well chosen.
3. **Run the self-test.** Actions → Clips → Run workflow → tick "Run the self-test". It builds a one-minute synthetic episode and a five-minute synthetic stream, clips both, and saves the results as the `selftest-clips` artifact on that run. Watch one of each. If the run's summary says "Picked by: claude", your credential works.
4. **Add your first client.** Copy `clients/_template.yml` to `clients/<short-name>.yml` and fill in the top half.
5. **Request clips.** Issues → New issue → New episode. Paste a Dropbox or Google Drive share link to the episode file, or a link to a Twitch or Kick broadcast.

## Day to day

**Request clips**: open a "New episode" issue. The fields are the client's short name, a link, and optionally a title, a clip count, and notes for the picker ("the pricing story around minute 30").

**Review**: the run comments on the issue with a table of clips and adds the `clipped` label. Links go to the release where the files live. `contact-sheet.jpg` shows one frame of each clip; `clips.md` has the post text and what is said in each.

**Redo**: Actions → Clips → Run workflow, with the issue number. Change the client file or the issue's notes first if you want a different result.

**When it fails**: the run comments with the reason and adds `failed`. Nothing retries a failed issue by itself.

**Episodes from a feed**: set `feed:` in a client file to their podcast RSS feed and the nightly run opens a request for each new episode. Feeds carry audio, so those clips are waveform videos.

**Labels**: `episode` → `processing` → `clipped` or `failed`.

### Getting the episode file

The link has to lead to the file itself, or to a Twitch or Kick broadcast. Reliable: a Dropbox or Google Drive link set to "anyone with the link", or a direct link to an `.mp4`, `.mov` or `.mp3`. Unreliable: a link to a YouTube or Spotify page. The pipeline will try, but video sites usually refuse downloads from cloud servers, so ask the client for the file.

## Streams on Twitch and Kick

**Reaching the sites works; clipping a real stream is untested.** On 9 October 2026 the link check passed on GitHub's servers for a Twitch channel and a Kick channel: each listed its broadcasts, opened one, and downloaded sound and picture from it. No real broadcast has been clipped end to end yet; that was tested on simulated broadcasts laid out the way the two sites serve theirs. Both sites change without notice, so run the link check again whenever something looks off.

**Check a link first.** Actions → Clips → Run workflow → paste a channel link or a broadcast link into "Or only test a Twitch or Kick link". In a couple of minutes the run's summary says, step by step, whether the channel's broadcasts can be listed, read and downloaded from GitHub's servers. Nothing is clipped.

**Clip one broadcast.** Paste its link into a "New episode" issue: `https://www.twitch.tv/videos/…` or `https://kick.com/<channel>/videos/…`. A channel link means that channel's latest finished broadcast.

**Watch a channel.** In the streamer's client file:

```yaml
kind: stream
twitch: some_channel
kick: some_channel
permission: Client agreement signed 2026-10-12
```

Each night the two newest broadcasts on each watched channel are looked at, and any that is finished, at least 15 minutes long, no more than four days old and not already filed gets a request of its own. A channel is not watched until `permission` is filled in.

### Whose streams

The pipeline can fetch any public broadcast. Whether you may post clips of it is a separate question, and the answer is the streamer's to give. They own what they broadcast, so permission to repost it has to come from them; do not assume a site's terms give it to you. Two arrangements make it clean: the streamer is your client, or the streamer runs a clipping program and you follow its rules. The `permission` line is where you write down which one applies, so that every channel clipped unattended has a recorded basis. Clips of someone who has not agreed can be taken down, and accounts that keep posting them can lose reach or monetization. None of this is legal advice.

Neither site offers outsiders an official way to download a broadcast. This goes through [yt-dlp](https://github.com/yt-dlp/yt-dlp), the same open-source downloader used for page links, and the sites' terms may not welcome that. A streamer can download their own broadcasts from their creator dashboard and share the file instead, which sidesteps the question.

### How a stream is handled

A stream is hours long and mostly not worth clipping, so it is never downloaded or transcribed whole.

1. **Skim.** Only the cheapest version is fetched: the sound alone on Twitch, the smallest picture on Kick, which offers no sound-only version. A quick speech model makes a rough transcript of all of it, and the loudness of every second is measured.
2. **Shortlist.** Claude reads the rough transcript, with marks where it got loud and where viewers made clips, and names the stretches worth a close look: about one and a half times as many as the clips wanted.
3. **Fetch.** Only those stretches are downloaded at full quality, each with some room either side.
4. **From here it is the same pipeline.** The stretches are transcribed properly, Claude picks the moments, and each is cut, framed and captioned. A clip always comes from a single stretch.

`kind: stream` in a client file sends any long recording down this route, including a file the streamer shares. Use it for anything over about two hours.

### What can go wrong

| What the issue says | What it means |
|---|---|
| refused the request, most likely its bot protection | The site blocked GitHub's server. It comes and goes, and Kick, which sits behind stricter bot protection, is the likelier of the two (it let GitHub through when this was written). Try later, or get the file from the streamer. |
| only shows this past broadcast to subscribers | The streamer restricts past broadcasts. They need to send the file. |
| has no past broadcast at that link | It was deleted. Twitch keeps past broadcasts 7 to 60 days depending on the account, and only if the streamer has switched on "Store past broadcasts". Kick keeps them 7 days, or 30 for verified channels. |
| still live | The broadcast has not ended. Run it again afterwards. The nightly check never files a broadcast that is still going. |

If the nightly run cannot check a watched channel at all, or finds one with no `permission` line, it opens an issue labelled `failed` saying so. It opens one, not one a night: while that issue stays open it stays quiet about that channel.

When Twitch or Kick changes something and every broadcast starts failing, the fix is nearly always a newer yt-dlp. Every run installs the newest one, so wait a few days and run the link check again.

## Time and cost

Measured on a two-core machine, which is what GitHub gives a private repo:

- Transcribing runs at about 3× real time with the default speech model, so a one-hour episode takes about 20 minutes.
- Rendering takes roughly as long as the clips are: eight 40-second clips, about 6 minutes.
- Skimming a stream runs at about 19× real time (measured on synthetic speech; real streams will differ).

So a one-hour episode costs around half an hour of Actions time, and a six-hour stream should cost about 45 minutes: 20 to skim it, the rest to fetch, transcribe and render the shortlist. That stream figure is an estimate; nothing that long has been run. A free GitHub account includes 2,000 minutes a month for private repos, which is about 60 such episodes or 40 such streams. A streamer who goes live every day uses most of that alone.

Claude is called once per episode with the transcript, and twice per stream. See [Claude's API pricing](https://claude.com/platform/api) for the API route; a subscription token counts against your plan's usage instead.

## How it makes its choices

- **Moments.** The transcript is split into sentences, also breaking where the audio really pauses. Claude gets the client's brief and the numbered sentences and returns stretches by number, so it cannot invent timestamps. Picks outside the length range or overlapping a better one are dropped. The instructions are in `prompts/pick.md`, `prompts/pick-stream.md` for streams, and `prompts/shortlist.md` for a stream's first pass.
- **Cut points.** Speech-model word timings can be a few tenths of a second off. Cuts are placed using the loudness of the audio itself, inside the pause before the first word and after the last. Long pauses inside a clip are shortened (`clips.tighten`), except on streams, where cutting time out of gameplay makes the picture jump.
- **Framing.** Each clip is scanned for camera cuts and faces. One person gets a 9:16 crop centred on them: tightened if they are small in frame, widened over a blurred fill if they sit right at the camera. Two people side by side are stacked one above the other. Anything else (no faces, a panel, a screen share) shows the whole picture over a blurred copy. The choice is made per camera shot and held steady for the shot. Force one with `clips.layout`.
- **Framing a stream.** A small face that stays put is taken to be the streamer's camera box. The clip is then split: the camera on top, the middle of the gameplay below. This is decided once for the whole clip, because game footage is full of sudden changes that would otherwise make the layout flicker. A scene with the camera full-screen is framed like any other talking head.
- **Captions.** Up to three words at a time with the spoken word highlighted, measured against the real font so a line never runs off the frame. The headline shows for the first few seconds, on two lines where it can, above the speaker's head where the footage leaves room. In the split layout both sit by the seam, clear of the face and of the middle of the game.
- **Sound.** Each clip is brought to −14 LUFS with one steady gain and a peak limiter.

## Settings

Everything per client lives in `clients/<short-name>.yml`; `clients/_template.yml` lists every option with its default. To try a setting once without editing the file:

```
python -m clipper run --client acme-show --source episode.mp4 --set clips.count=3 --set captions.uppercase=false
```

## Run it on your own computer

Needs Python 3.12 or 3.13 and ffmpeg.

```
pip install -r requirements.txt
python -m clipper run --client demo --source path/or/link/to/episode.mp4 --work work
python -m clipper check --source https://www.twitch.tv/some_channel
```

Results land in `work/out`. If Claude Code is installed and signed in, the picker uses it; otherwise set `ANTHROPIC_API_KEY`, or pass `--picker heuristic`.

Tests: `python -m pytest` (a few seconds). `CLIPPER_SLOW=1 python -m pytest tests/test_end_to_end.py` renders real clips from synthetic episodes and a synthetic stream (about fourteen minutes on two cores).

## Limits worth knowing

- **Not yet proven on a real show or a real stream.** Framing, cut detection and captions were checked on synthetic episodes and on a few short test clips of real people, including a multi-camera edit. The stream route has only ever seen a simulated stream. A real studio, a real game and real cross-talk will find things those did not. Watch the first few episodes' clips closely.
- **Captions can mishear.** The speech model gets most words right and still slips on names, jargon and mumbled words, and more so over game sound and music. Skim "What is said" in `clips.md` before a clip goes to a client. Fix repeat offenders in the client file with `transcribe.vocabulary` and `transcribe.replace`.
- **English by default.** Other languages need `transcribe.model: small` (not `small.en`), `stream.scan_model: tiny` and `transcribe.language`. Untested.
- **Moments are found from what is said.** A silent clutch play, however good, is invisible to it. Loud moments and viewer clips are used as hints, but a stretch where nobody speaks is never shortlisted.
- **The camera box is found by the face in it.** A streamer with no camera gets the whole picture over a blurred copy. A very large camera box may be taken for a full-screen scene, and a facecam the detector cannot see (a mask, a VTuber avatar) will not be found. Force the layout with `clips.layout` when it guesses wrong.
- **One layout per shot.** Framing does not follow someone who walks around, and does not switch between two people in a single wide shot based on who is talking; it stacks them.
- **Viewer-clip hints are rough.** They are placed half a minute before the time each clip was made, which is a guess, and how the sites report those times was not checked.
- **HDR phone footage** is converted without tone mapping and can look washed out.
- **Episode size.** A file can be up to 8 GB, and up to 8 hours unless its client is `kind: stream`. Only the first 10 hours of a stream are skimmed (`stream.max_hours`). A run stops at 5.5 hours.
- **The headline is Claude's.** It is told to promise only what the clip delivers. Read them before a client does.

## Security notes

- Only repo collaborators can start a run. Issues from anyone else are ignored.
- The transcript is untrusted text. The two steps that send it to Claude have no GitHub token, and Claude gets no tools; its reply is checked and reduced to line numbers and short strings before anything acts on it.
- Issue text and workflow inputs never reach a shell. They are read through the GitHub API or passed as data, and parsed in Python.
- A broadcast's playlist address carries an access token. It is never written to disk or to the log.
- Each job asks only for the permissions it uses (`issues: write`, plus `contents: write` to attach clips).
- Episode files and clips are never committed. `.gitignore` blocks media, and outputs go to releases.

## What is where

```
clipper/            the pipeline
  ingest.py           download, check, extract audio
  streams.py          Twitch and Kick: reading links, listing a channel's broadcasts
  hls.py              stream playlists: reading them, fetching only the stretches needed
  scan.py             first pass over a stream: rough transcript, loud moments, shortlist
  parts.py            fetch the shortlisted stretches and lay them end to end; the link check
  transcribe.py       speech to timed words (faster-whisper)
  silence.py          real pauses in the audio; cut points; trimming dead air
  pick.py             Claude picker, fallback picker, checks on the reply
  reframe.py          camera cuts, faces, layout per shot
  captions.py         caption and headline file (ASS)
  render.py           ffmpeg graph and encode
  package.py          manifest and review sheet
  feeds.py            podcast RSS
  ci.py               GitHub glue: plan, start, publish, fail
prompts/            what the picker is told (pick.md, pick-stream.md, shortlist.md)
clients/            one file per client
muse/prompts.md     operator prompts for an agent
assets/             caption font and face-detection model (see assets/NOTICE.md)
tests/              unit tests, sample generators, self-test check
.github/            workflow, shared setup action, issue form
```
