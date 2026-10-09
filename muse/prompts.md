# Operator prompts for Meta Muse

Copy-paste prompts for running the service around the pipeline: finding clients, filing episodes, delivering clips, reporting, invoicing.

**These are drafts and have not been run.** Muse was a month old when they were written, and Meta has not documented what several of its connectors can do (GitHub in particular). Run each one once by hand and watch what it does before letting it repeat. Where Muse cannot do a step, the prompts tell it to stop and say so.

## How Muse and the repo fit together

Muse never edits files in the repo. It only **opens issues and reads comments**:

| Muse opens an issue... | ...and then |
|---|---|
| labelled `episode`, in the format below | the Clips workflow makes the clips and comments with links |
| labelled `client` | you ask Claude to turn it into `clients/<short-name>.yml` |
| labelled `results` | you ask Claude to fold it into that client's `notes` |

**Episode request format.** Title: `Episode: <episode title>`. Label: `episode`. Body, exactly:

```
### Client

<short-name>

### Episode link

<share link to the file, or a link to one past broadcast on Twitch or Kick>

### Episode title

<episode title>

### Notes

<anything the picker should know, or leave empty>
```

**Streamers need no filing.** Once a streamer's client file names their Twitch or Kick channel, the nightly run files each new broadcast by itself. Muse only files a stream by hand for a one-off, such as a broadcast from before the channel was added.

## 0. Standing rules (give Muse this first, once)

```
You help me run a small clipping service: I turn clients' long episodes and live streams into short clips for their own channels. Remember these rules for everything you do for it.

1. Never send an email or message, publish a post, or spend money without showing me exactly what will go out and getting my yes for that specific item.
2. A client's footage and clips go only to that client and to accounts that client controls. Never post one client's material anywhere else, and never to my own accounts, unless I tell you that streamer's own clipping program allows it and show you its rules.
3. In the GitHub repository [OWNER/REPO] you may open issues, read issues, comments and releases, and download release files. Do not change, add or delete any file, branch, setting or secret there.
4. If a step needs a connection or permission you do not have, stop and tell me what is missing. Do not find another way around.
5. Keep a running list called "Clipping service log" of what you did for each client and when.

Connect these if they are not connected: GitHub (only the repository above), Gmail, Dropbox, Google Sheets, Stripe, and my Meta business accounts. Tell me which ones you could not connect.
```

## 1. Find prospects (weekly)

For podcasts and interview shows:

```
Every Monday morning, find 10 podcasts or interview shows that could use a clipping service, and add them to my Google Sheet "Clip prospects" (create it if missing; columns: Show, Host, Link, Contact email, Where they publish, Episodes per month, Short clips posted in the last 30 days, Why they fit, Status).

A good fit: [NICHE, e.g. business, tech or security shows in English], publishes a new long episode at least twice a month, has posted fewer than 4 short clips in the last 30 days, and lists a contact email publicly. Skip any show already in the sheet.

Then draft a short first email to the 5 best fits. Each one must name a specific recent episode and one moment in it worth clipping, say plainly that I am writing cold, say what I do in one sentence, and make it easy to say no. No follow-up is promised or implied. Put the drafts in my Gmail drafts and show them to me. Do not send anything.
```

For streamers:

```
Every Monday morning, find 10 Twitch or Kick streamers who could use a clipping service, and add them to my Google Sheet "Clip prospects" on a tab called "Streamers" (columns: Streamer, Platform, Channel link, Business email, What they stream, Streams per week, Usual live viewers, Short clips posted in the last 30 days and where, Why they fit, Status).

A good fit: streams in English at least three times a week, [GAME OR CATEGORY, e.g. ranked shooters, or Just Chatting], usually has between 50 and 2,000 people watching (enough happening to clip, not yet big enough to have an editor), talks through what they are doing instead of playing in silence, has posted fewer than 4 short clips anywhere in the last 30 days, and lists a business email publicly, on their channel page or a linked profile. Skip anyone already in the sheet, and anyone who says they do not want business contact.

Then draft a short first email to the 5 best fits. Each one must mention one specific recent stream and what happened in it that would make a good clip, say plainly that I am writing cold, say in one sentence that I cut their streams into captioned vertical clips for their own TikTok, Shorts and Reels, and make it easy to say no. Put the drafts in my Gmail drafts and show them to me. Do not send anything, and do not message anyone through Twitch, Kick or Discord.
```

Optional, once the pipeline has run cleanly for a while: have it file an episode request for a prospect's latest episode with client `spec`, so the pitch can carry two finished samples. Those samples are for that show's owner only and must never be posted.

## 2. Take on a new client

A podcast or show:

```
I have a new clipping client: [SHOW NAME], contact [NAME, EMAIL].

1. Create a Dropbox folder "Clip clients/[SHOW NAME]" with subfolders "Episodes" and "Clips". Create a file request on "Episodes" so the client can upload without a Dropbox account, and a view-only shared link to "Clips".
2. Draft a welcome email to the contact with the upload link, and these questions: Who is the show for, in a sentence or two? How should captions and post text sound? Any topics never to clip? Brand colours or a font for captions? Names and terms that are often misspelled? Which accounts will the clips be posted to, and who posts them? Show me the draft; do not send it.
3. When they reply, open an issue in [OWNER/REPO] titled "New client: [SHOW NAME]" with the label "client", containing their answers word for word and the two Dropbox links. Then tell me it is ready.
```

A streamer:

```
I have a new clipping client who is a streamer: [STREAMER NAME], contact [NAME, EMAIL].

1. Create a Dropbox folder "Clip clients/[STREAMER NAME]" with a subfolder "Clips", and a view-only shared link to "Clips".
2. Draft a welcome email to the contact with these questions: What are the links to your Twitch and Kick channels? Do you confirm in writing that I may fetch your past broadcasts from those channels and cut clips from them for you? On Twitch, is "Store past broadcasts" switched on in your stream settings (without it there is nothing for me to fetch), and are past broadcasts open to everyone or to subscribers only? Do you have a camera on screen, and where? How many clips do you want from each stream? How should captions and post text sound? Any games, people or topics never to clip? Names, slang and game terms that are often misspelled? Which accounts will the clips be posted to, and who posts them? Show me the draft; do not send it.
3. When they reply, open an issue in [OWNER/REPO] titled "New client: [STREAMER NAME]" with the label "client". Put their answers in it word for word, the Dropbox link, and one line that starts "Permission:" followed by their exact words giving permission and the date they wrote them. If they did not clearly give permission, say so in the issue instead and tell me.
```

Then, in Claude: "Add the client from issue #N." For a streamer, also run the link check on their channel before the first night (Actions → Clips → Run workflow → "only test a Twitch or Kick link").

## 3. File new episodes (daily, for clients who upload files)

```
Every weekday morning, look in each "Clip clients/*/Episodes" folder in my Dropbox for video or audio files added since you last checked.

For each new file: create a shared link to it (anyone with the link can view), then open an issue in [OWNER/REPO] in the episode request format I gave you, using that client's short name, the link, and the file name without its extension as the title. Add the label "episode".

Do not file the same file twice; check your log first. Then tell me what you filed.
```

## 4. Deliver finished clips (daily)

```
Every weekday afternoon, look in [OWNER/REPO] for issues labelled "clipped" that do not have the label "delivered".

For each one: read the comment that lists the clips. Download the clip files, contact-sheet.jpg and clips.md from the linked release into that client's Dropbox "Clips" folder, in a new subfolder named with today's date and the episode title. If you cannot download them, stop and tell me.

Then send me the contact sheet and the list of headlines, and ask which clips to send. When I answer, draft an email to the client's contact with the Dropbox link and, for each clip I chose, its headline and its post text copied from clips.md. Show me the draft. After I approve and it is sent, add the label "delivered" to the issue.

If an issue is labelled "failed", tell me what its last comment says. Do not try to fix it.
```

## 5. Post for a client (only where they have given you access)

```
For [SHOW NAME], whose Instagram and Facebook accounts I manage through my Meta business account: take the clips I approved from [EPISODE] and prepare them as Reels, one per day at [TIME, TIMEZONE], each with its post text from clips.md.

Show me each post (clip, caption, account, time) before scheduling it, and schedule only the ones I approve. Only post to accounts that belong to [SHOW NAME].
```

## 6. Report what worked (weekly)

```
Every Friday, for each client whose accounts are connected to my Meta business account, look at the clips posted in the last 14 days: plays, average watch time, shares, saves and follows from each.

Open one issue per client in [OWNER/REPO] titled "Results: [short-name], week of [DATE]" with the label "results". In it: the three best and two worst clips by watch time with their headlines and numbers, and two or three plain observations about what the strong ones share (how they open, how long they are, what they are about). Report only what the numbers show; if there are too few posts to say anything, say that.

Send me a three-line summary per client.
```

Then, in Claude: "Fold the open results issues into the client notes."

## 7. Invoice (monthly)

```
On the 1st of each month, for each active client in my "Clipping service log": count the episodes and streams delivered last month (issues labelled "delivered" in [OWNER/REPO]) and prepare a Stripe invoice for [THEIR PLAN, e.g. $X per month for up to N episodes], with one line per episode delivered. Show me every invoice before it is sent. Send only the ones I approve.
```

## Clipping programs

Some streamers pay people per view to post clips of them, through a marketplace or their own Discord. Clipping for one of those is not a client relationship: the clips go on your accounts, and the streamer's published rules are what give you permission. If you join one:

- Make a client file for that streamer with `kind: stream`, their channel, and `permission: Clipping program rules: <link to the rules>, joined <date>`.
- Read the rules for what they require and forbid (which platforms, tags or credit lines, minimum length, what counts for payment, whether the post must say it is paid). Put the ones that affect the clip itself in the client file's `about` and `avoid`, and follow the rest when posting.
- Have Muse post only after you approve each clip, as in prompt 5, to the accounts the program accepts.
- A program can change its rules or end. Check them again before each payout period.
