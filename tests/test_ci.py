import json

import pytest

from clipper import ci
from clipper.util import ClipperError

FORM = """### Client

acme-show

### Episode link

https://www.dropbox.com/scl/fi/abc123/ep42.mp4?rlkey=xyz&dl=0

### Episode title

Ep 42: Pricing with Dana

### Number of clips

5

### Notes

Look for the part about raising prices.
"""


def issue(**overrides) -> dict:
    base = {"number": 7, "state": "open", "title": "Episode: 42", "body": FORM, "author_association": "OWNER",
            "user": {"login": "trevor"}, "labels": [{"name": "episode"}]}  # fmt: skip
    base.update(overrides)
    return base


class FakeGitHub:
    """Stands in for the `gh` CLI: records every call and answers from canned data."""

    def __init__(self, issues=None):
        self.calls = []
        self.issues = {i["number"]: i for i in issues or []}

    def __call__(self, *args, check=True, input=None):
        self.calls.append(args)
        if args[:1] == ("api",) and "/issues/" in args[1]:
            return json.dumps(self.issues[int(args[1].rsplit("/", 1)[1])])
        if args[:1] == ("api",) and "--jq" in args and args[args.index("--jq") + 1] == ".[].body":
            return "\n".join(i.get("body") or "" for i in self.issues.values())
        if args[:1] == ("api",):
            rows = [
                {"number": i["number"], "state": i["state"], "author_association": i["author_association"],
                 "user": {"login": i["user"]["login"]}, "pull_request": bool(i.get("pull_request")),
                 "labels": [label["name"] for label in i["labels"]]}
                for i in self.issues.values() if i["state"] == "open"
            ]  # fmt: skip
            return "\n".join(json.dumps(r) for r in rows)
        if args[:2] == ("issue", "create"):
            return "https://github.com/me/clips/issues/99\n"
        return ""

    def said(self, *prefix) -> list[tuple]:
        return [c for c in self.calls if c[: len(prefix)] == prefix]


@pytest.fixture
def github(monkeypatch, tmp_path):
    fake = FakeGitHub()
    monkeypatch.setattr(ci, "gh", fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/clips")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    fake.output = tmp_path / "output.txt"
    return fake


def outputs(github) -> dict:
    return dict(line.split("=", 1) for line in github.output.read_text().splitlines())


# ------------------------------------------------------------ reading issues


def test_form_issue_is_read_in_full():
    request = ci.parse_issue("Episode: 42", FORM)
    assert request == {
        "client": "acme-show",
        "source": "https://www.dropbox.com/scl/fi/abc123/ep42.mp4?rlkey=xyz&dl=0",
        "title": "Ep 42: Pricing with Dana",
        "notes": "Look for the part about raising prices.",
        "count": 5,
    }


def test_empty_form_fields_fall_back_sensibly():
    body = "### Client\n\nacme-show\n\n### Episode link\n\nhttps://x.test/a.mp4\n\n### Episode title\n\n_No response_\n\n### Number of clips\n\n_No response_\n\n### Notes\n\n_No response_\n"
    request = ci.parse_issue("Episode: the big one", body)
    assert request["title"] == "Episode: the big one" and request["notes"] == "" and request["count"] is None


def test_an_untouched_form_title_counts_as_no_title():
    body = "### Client\n\nacme-show\n\n### Episode link\n\nhttps://www.twitch.tv/videos/2345678901\n\n### Episode title\n\n_No response_\n"
    assert ci.parse_issue("Episode: ", body)["title"] == "" and ci.parse_issue("Episode", body)["title"] == ""
    assert ci.parse_issue("Episode: 42", body)["title"] == "Episode: 42"


def test_freehand_issue_with_key_value_lines():
    body = "Client: acme-show\nEpisode link: https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp=sharing\nTitle: Ep 43\nNotes: skip the ad read"
    request = ci.parse_issue("new one", body)
    assert request["client"] == "acme-show" and request["title"] == "Ep 43" and request["notes"] == "skip the ad read"
    assert request["source"].startswith("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view")


def test_bare_link_and_client_label():
    request = ci.parse_issue("Ep 44", "Here it is: https://x.test/ep44.mp4.", labels={"episode", "client:acme-show"})
    assert request["client"] == "acme-show" and request["source"] == "https://x.test/ep44.mp4" and request["title"] == "Ep 44"


def test_only_collaborators_and_the_workflow_itself_are_trusted():
    assert ci.eligible(issue())
    assert not ci.eligible(issue(author_association="NONE"))
    assert ci.eligible(issue(author_association="NONE", user={"login": "github-actions[bot]"}))
    assert not ci.eligible(issue(labels=[{"name": "episode"}, {"name": "clipped"}]))
    assert not ci.eligible(issue(state="closed"))
    assert not ci.eligible(issue(pull_request={"url": "x"}))
    assert not ci.eligible(issue(labels=[], body="just a question about the repo"))


def test_form_shaped_issue_counts_even_without_the_label():
    assert ci.eligible(issue(labels=[]))
    assert not ci.eligible(issue(labels=[], author_association="NONE"))


# ---------------------------------------------------------------------- plan


def run_plan(monkeypatch, tmp_path, event: str, payload: dict) -> None:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    ci.cmd_plan(None)


def test_plan_for_a_new_issue(github, monkeypatch, tmp_path):
    run_plan(monkeypatch, tmp_path, "issues", {"issue": issue()})
    assert outputs(github) == {"issues": "[7]", "count": "1"}
    assert len(github.said("label", "create")) == len(ci.LABELS)


def test_plan_ignores_issues_from_strangers(github, monkeypatch, tmp_path):
    run_plan(monkeypatch, tmp_path, "issues", {"issue": issue(author_association="NONE")})
    assert outputs(github) == {"issues": "[]", "count": "0"}


def test_plan_labels_a_form_issue_that_arrived_without_one(github, monkeypatch, tmp_path):
    run_plan(monkeypatch, tmp_path, "issues", {"issue": issue(labels=[])})
    assert outputs(github)["issues"] == "[7]"
    assert ("issue", "edit", "7", "--add-label", "episode") in github.calls


def test_nightly_plan_takes_what_is_waiting_oldest_first(github, monkeypatch, tmp_path):
    github.issues = {
        3: issue(number=3),
        4: issue(number=4, labels=[{"name": "episode"}, {"name": "clipped"}]),
        5: issue(number=5, labels=[{"name": "episode"}, {"name": "failed"}]),
        6: issue(number=6, author_association="NONE"),
        8: issue(number=8, labels=[{"name": "episode"}, {"name": "processing"}]),
    }
    run_plan(monkeypatch, tmp_path, "schedule", {})
    assert outputs(github) == {"issues": "[3, 8]", "count": "2"}


def test_nightly_plan_is_capped(github, monkeypatch, tmp_path):
    github.issues = {n: issue(number=n) for n in range(1, 12)}
    run_plan(monkeypatch, tmp_path, "schedule", {})
    assert json.loads(outputs(github)["issues"]) == list(range(1, ci.MAX_PER_RUN + 1))


def test_manual_redo_clears_the_old_state(github, monkeypatch, tmp_path):
    run_plan(monkeypatch, tmp_path, "workflow_dispatch", {"inputs": {"issue": "12", "client": "", "source": ""}})
    assert outputs(github)["issues"] == "[12]"
    (edit,) = github.said("issue", "edit", "12")
    assert "clipped" in edit and "failed" in edit and "processing" in edit


def test_manual_new_episode_opens_an_issue(github, monkeypatch, tmp_path):
    run_plan(monkeypatch, tmp_path, "workflow_dispatch", {"inputs": {"issue": "", "client": "demo", "source": "https://x.test/a.mp4", "title": "Ep 1"}})
    assert outputs(github)["issues"] == "[99]"
    (create,) = github.said("issue", "create")
    body = create[create.index("--body") + 1]
    assert ci.parse_issue("", body)["client"] == "demo" and "https://x.test/a.mp4" in body


def test_new_feed_episodes_become_issues_once(github, monkeypatch, tmp_path):
    cfg = {"name": "Acme Show", "feed": "https://feeds.test/acme.rss"}
    monkeypatch.setattr(ci.config, "list_clients", lambda: ["acme-show"])
    monkeypatch.setattr(ci.config, "load_client", lambda slug: cfg)
    monkeypatch.setattr(ci.feeds, "fetch_feed", lambda url: b"")
    monkeypatch.setattr(ci.feeds, "parse_feed", lambda data: [])
    monkeypatch.setattr(ci.feeds, "recent", lambda episodes: [
        {"key": "a" * 16, "title": "Ep 50", "source": "https://cdn.test/50.mp3"},
        {"key": "b" * 16, "title": "Ep 49", "source": "https://cdn.test/49.mp3"},
    ])  # fmt: skip
    github.issues = {2: issue(number=2, state="closed", body=f"old\n<!-- feed-item:{'b' * 16} -->")}
    assert ci.new_from_feeds() == [99]
    (create,) = github.said("issue", "create")
    assert create[create.index("--title") + 1] == "Acme Show: Ep 50"
    assert f"feed-item:{'a' * 16}" in create[create.index("--body") + 1]


# --------------------------------------------------------------------- start


class Args:
    def __init__(self, work, issue=7):
        self.work, self.issue = work, issue


def test_start_writes_the_episode_and_marks_it_in_progress(github, tmp_path):
    github.issues = {7: issue(body=FORM.replace("acme-show", "demo"))}
    ci.cmd_start(Args(tmp_path / "work"))
    episode = json.loads((tmp_path / "work" / "episode.json").read_text())
    assert episode["client"] == "demo" and episode["issue"] == 7 and episode["title"] == "Ep 42: Pricing with Dana"
    assert episode["overrides"] == {"clips": {"count": 5}} and episode["notes"].startswith("Look for")
    assert outputs(github) == {"skip": "false", "model": "tiny.en"}
    assert github.said("issue", "edit", "7", "--add-label", "processing")


def test_start_skips_finished_and_failed_issues(github, tmp_path):
    for state in ("clipped", "failed"):
        github.issues = {7: issue(labels=[{"name": "episode"}, {"name": state}])}
        github.output.write_text("")
        ci.cmd_start(Args(tmp_path / "work"))
        assert outputs(github) == {"skip": "true"}


def test_start_does_not_silently_retry_a_run_that_died(github, tmp_path):
    github.issues = {7: issue(labels=[{"name": "episode"}, {"name": "processing"}])}
    with pytest.raises(ClipperError, match="stopped without finishing"):
        ci.cmd_start(Args(tmp_path / "work"))


def test_start_explains_an_unknown_client_and_a_missing_link(github, tmp_path):
    github.issues = {7: issue()}  # acme-show has no file in clients/
    with pytest.raises(ClipperError, match="No client file at clients/acme-show.yml"):
        ci.cmd_start(Args(tmp_path / "work"))
    github.issues = {7: issue(body="### Client\n\ndemo\n\n### Episode link\n\n_No response_\n")}
    with pytest.raises(ClipperError, match="no link"):
        ci.cmd_start(Args(tmp_path / "work"))


def test_a_failed_command_leaves_its_reason_for_the_comment(github, tmp_path, monkeypatch):
    github.issues = {7: issue()}
    monkeypatch.setattr("sys.argv", ["clipper.ci"])
    assert ci.main(["start", "--issue", "7", "--work", str(tmp_path / "work")]) == 1
    assert "No client file" in (tmp_path / "work" / "error.txt").read_text()


# ------------------------------------------------------------ publish / fail

MANIFEST = {
    "client": "demo", "show": "The Demo Show", "title": "Ep 42", "source": "x", "picker": "claude",
    "model": "claude-sonnet-5-5", "note": "",
    "clips": [
        {"rank": 1, "file": "01-charge-more.mp4", "title": "Charge | more", "duration": 42.3, "source_start": 751.0,
         "source_end": 795.0, "trimmed": 1.2, "post": "Raise your prices.\n\n#pricing", "why": "Clear claim.", "text": "..."},
    ],
}  # fmt: skip


def test_comment_lists_clips_with_links_and_post_text():
    body = ci.comment_body(MANIFEST, "https://github.com/me/clips/releases/tag/clips-7", "https://github.com/me/clips/releases/download/clips-7")
    assert "### 1 clip ready" in body and "Picked by Claude (claude-sonnet-5-5)." in body
    assert "| 1 | Charge \\| more | 0:42 | 12:31 | [01-charge-more.mp4](https://github.com/me/clips/releases/download/clips-7/01-charge-more.mp4) |" in body
    assert "Raise your prices." in body and "#pricing" in body and "_Why:_ Clear claim." in body
    noted = ci.comment_body({**MANIFEST, "picker": "heuristic", "note": "No Claude credential is set."}, "r", "d")
    assert "the built-in fallback picker" in noted and "> **Note:** No Claude credential is set." in noted


def test_publish_makes_a_release_comments_and_relabels(github, tmp_path):
    out = tmp_path / "work" / "out"
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps(MANIFEST))
    (out / "clips.md").write_text("# sheet")
    (out / "01-charge-more.mp4").write_bytes(b"x")
    ci.cmd_publish(Args(tmp_path / "work"))
    (create,) = github.said("release", "create")
    assert create[2] == "clips-7" and create[3].endswith("01-charge-more.mp4") and "Clips: The Demo Show — Ep 42" in create
    assert github.said("issue", "comment", "7")
    (edit,) = github.said("issue", "edit", "7")
    assert edit[edit.index("--add-label") + 1] == "clipped" and "processing" in edit
    order = [c[:2] for c in github.calls]
    assert order.index(("release", "create")) < order.index(("issue", "comment")) < order.index(("issue", "edit"))


def test_fail_reports_the_reason_and_marks_the_issue(github, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    work = tmp_path / "work"
    work.mkdir()
    (work / "error.txt").write_text("**Getting the episode failed.** The episode link answered 404.")
    ci.cmd_fail(Args(work))
    (comment,) = github.said("issue", "comment", "7")
    body = comment[comment.index("--body") + 1]
    assert "The episode link answered 404." in body and "actions/runs/555" in body
    (edit,) = github.said("issue", "edit", "7")
    assert edit[edit.index("--add-label") + 1] == "failed"


def test_client_is_inferred_only_when_there_is_exactly_one_real_one(monkeypatch):
    monkeypatch.setattr(ci.config, "list_clients", lambda: ["acme-show", "demo", "spec"])
    assert ci.resolve_client("") == "acme-show" and ci.resolve_client("other") == "other"
    monkeypatch.setattr(ci.config, "list_clients", lambda: ["acme-show", "beta-cast", "demo"])
    with pytest.raises(ClipperError, match="does not say which client"):
        ci.resolve_client("")
    monkeypatch.setattr(ci.config, "list_clients", lambda: ["demo", "spec"])
    with pytest.raises(ClipperError, match="none yet"):
        ci.resolve_client("")


# ------------------------------------------------- Twitch and Kick channels

NOW = 1_790_100_000.0


def watch(monkeypatch, clients: dict, listings: dict, broadcasts: dict):
    """Stand in for the client files and for what Twitch and Kick would answer."""
    from clipper import config as real_config
    from clipper.streams import Broadcast

    def load(slug):
        return real_config.merge(real_config.DEFAULTS, {"name": slug.title(), **clients[slug]})

    def recent(platform, channel, limit=3):
        answer = listings[(platform, channel)]
        if isinstance(answer, Exception):
            raise answer
        return answer[:limit]

    def resolve(url):
        answer = broadcasts[url]
        if isinstance(answer, Exception):
            raise answer
        return Broadcast(**answer)

    monkeypatch.setattr(ci.config, "list_clients", lambda: sorted(clients))
    monkeypatch.setattr(ci.config, "load_client", load)
    monkeypatch.setattr(ci.streams, "recent", recent)
    monkeypatch.setattr(ci.streams, "resolve", resolve)


def vod(n: int, **overrides) -> dict:
    base = {"platform": "twitch", "id": f"v{n}", "url": f"https://www.twitch.tv/videos/{n}", "title": f"Stream {n}",
            "channel": "some_body", "duration": 7200.0, "started": NOW - 20_000, "live": False}  # fmt: skip
    base.update(overrides)
    return base


def listed(*numbers) -> list[dict]:
    return [{"id": f"v{n}", "url": f"https://www.twitch.tv/videos/{n}", "title": f"Stream {n}", "duration": 7200.0,
             "started": None, "live": False} for n in numbers]  # fmt: skip


def test_a_new_broadcast_on_a_watched_channel_becomes_an_issue_once(github, monkeypatch):
    watch(monkeypatch, {"streamer": {"kind": "stream", "twitch": "some_body", "permission": "Client agreement, 2026-10-01"}},
          {("twitch", "some_body"): listed(9, 8)}, {"https://www.twitch.tv/videos/9": vod(9), "https://www.twitch.tv/videos/8": vod(8)})  # fmt: skip
    github.issues = {2: issue(number=2, state="closed", body="### Episode link\n\nx\n<!-- stream-item:twitch-v8 -->")}
    assert ci.new_from_channels(now=NOW) == [99]
    (create,) = github.said("issue", "create")
    body = create[create.index("--body") + 1]
    assert create[create.index("--title") + 1] == "Streamer: Stream 9"
    assert "https://www.twitch.tv/videos/9" in body and "<!-- stream-item:twitch-v9 -->" in body
    assert ci.parse_issue("", body)["client"] == "streamer" and ci.parse_issue("", body)["source"] == "https://www.twitch.tv/videos/9"


def test_a_channel_is_not_watched_until_permission_is_recorded(github, monkeypatch, caplog):
    watch(monkeypatch, {"streamer": {"kind": "stream", "twitch": "some_body", "permission": "  "}},
          {("twitch", "some_body"): listed(9)}, {"https://www.twitch.tv/videos/9": vod(9)})  # fmt: skip
    with caplog.at_level("WARNING"):
        assert ci.new_from_channels(now=NOW) == []
    assert "no `permission` line" in caplog.text
    # Nothing is clipped, and the operator is told once, in an issue, why nothing is happening.
    (create,) = github.said("issue", "create")
    body = create[create.index("--body") + 1]
    assert create[create.index("--title") + 1] == "Not watching some_body on Twitch yet" and create[create.index("--label") + 1] == "failed"
    assert "clients/streamer.yml" in body and "<!-- notice:permission-streamer -->" in body
    assert not ci.eligible({"number": 5, "state": "open", "body": body, "labels": [{"name": "failed"}], "author_association": "NONE",
                            "user": {"login": "github-actions[bot]"}})  # and the note is never mistaken for an episode
    github.issues = {5: issue(number=5, labels=[{"name": "failed"}], body=body)}
    github.calls.clear()
    ci.new_from_channels(now=NOW)
    assert not github.said("issue", "create")  # still open: not raised twice


def test_a_channel_that_cannot_be_checked_is_reported_once(github, monkeypatch):
    from clipper.util import ClipperError as Problem

    watch(monkeypatch, {"streamer": {"kind": "stream", "kick": "somebody", "permission": "Client agreement"}},
          {("kick", "somebody"): Problem("Kick refused the request from this server, most likely its bot protection.")}, {})  # fmt: skip
    assert ci.new_from_channels(now=NOW) == []
    (create,) = github.said("issue", "create")
    body = create[create.index("--body") + 1]
    assert create[create.index("--title") + 1] == "Could not check somebody on Kick"
    assert "bot protection" in body and "https://kick.com/somebody" in body and "<!-- notice:check-kick-somebody -->" in body
    github.issues = {6: issue(number=6, labels=[{"name": "failed"}], body=body)}
    github.calls.clear()
    assert ci.new_from_channels(now=NOW) == [] and not github.said("issue", "create")


def test_broadcasts_that_are_live_short_old_or_unreadable_are_left_alone(github, monkeypatch):
    from clipper.util import ClipperError as Problem

    watch(
        monkeypatch,
        {"streamer": {"kind": "stream", "twitch": "some_body", "kick": "somebody", "permission": "Clipping program rules: https://x.test/rules"}},
        {("twitch", "some_body"): listed(5, 4), ("kick", "somebody"): Problem("Kick refused the request")},
        {"https://www.twitch.tv/videos/5": vod(5, live=True), "https://www.twitch.tv/videos/4": vod(4, duration=300.0)},
    )
    monkeypatch.setattr(ci, "BROADCASTS_PER_CHECK", 5)
    assert ci.new_from_channels(now=NOW) == []  # one still being recorded, one only five minutes long, Kick unreachable
    assert [c[c.index("--title") + 1] for c in github.said("issue", "create")] == ["Could not check somebody on Kick"]
    github.calls.clear()

    watch(monkeypatch, {"streamer": {"kind": "stream", "twitch": "some_body", "permission": "ok"}},
          {("twitch", "some_body"): listed(3, 2, 1)},
          {"https://www.twitch.tv/videos/3": vod(3, started=NOW - 10 * 86400), "https://www.twitch.tv/videos/2": Problem("Twitch refused"),
           "https://www.twitch.tv/videos/1": vod(1)})  # fmt: skip
    assert ci.new_from_channels(now=NOW) == [99]  # ten days old: skipped. Unreadable: skipped. The third is taken.
    (create,) = github.said("issue", "create")
    assert "videos/1" in create[create.index("--body") + 1]


def test_only_the_newest_few_broadcasts_are_looked_at(github, monkeypatch):
    watch(monkeypatch, {"streamer": {"kind": "stream", "twitch": "some_body", "permission": "ok"}},
          {("twitch", "some_body"): listed(9, 8, 7, 6)}, {f"https://www.twitch.tv/videos/{n}": vod(n) for n in (9, 8, 7, 6)})  # fmt: skip
    assert len(ci.new_from_channels(now=NOW)) == ci.BROADCASTS_PER_CHECK == 2  # adding a channel never back-fills its archive


def test_clients_without_channels_cost_nothing(github, monkeypatch):
    watch(monkeypatch, {"podcast": {"feed": ""}}, {}, {})
    assert ci.new_from_channels(now=NOW) == [] and github.calls == []


def test_the_nightly_plan_checks_channels_too(github, monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "new_from_feeds", lambda: [])
    monkeypatch.setattr(ci, "new_from_channels", lambda: [41])
    github.issues = {3: issue(number=3)}
    run_plan(monkeypatch, tmp_path, "schedule", {})
    assert outputs(github)["issues"] == "[41, 3]"


def test_start_names_both_speech_models_for_a_stream(github, tmp_path, monkeypatch):
    from clipper import config as real_config

    cfg = real_config.merge(real_config.DEFAULTS, {"name": "Streamer", "kind": "stream"})
    monkeypatch.setattr(ci.config, "load_client", lambda slug: cfg)
    github.issues = {7: issue(body="### Client\n\nstreamer\n\n### Episode link\n\nhttps://www.twitch.tv/videos/2345678901\n")}
    ci.cmd_start(Args(tmp_path / "work"))
    assert outputs(github) == {"skip": "false", "model": "small.en+tiny.en"}
    assert json.loads((tmp_path / "work" / "episode.json").read_text())["source"] == "https://www.twitch.tv/videos/2345678901"
    # A stream link sent to an ordinary show is still skimmed, so it needs the quick model too.
    cfg["kind"] = "show"
    github.output.write_text("")
    ci.cmd_start(Args(tmp_path / "work"))
    assert outputs(github)["model"] == "small.en+tiny.en"


def test_the_comment_for_a_stream_says_so_and_links_each_moment():
    manifest = {**MANIFEST, "recording": {"platform": "twitch", "channel": "some_body", "length": 21480.0, "skimmed": 21480.0, "stretches": 14},
                "clips": [{**MANIFEST["clips"][0], "source_start": 7384.0, "watch": "https://www.twitch.tv/videos/9?t=2h3m4s"}]}  # fmt: skip
    body = ci.comment_body(manifest, "r", "d")
    assert "From a 5:58:00 stream. 14 stretches of it were looked at closely." in body
    assert "| [2:03:04](https://www.twitch.tv/videos/9?t=2h3m4s) |" in body
