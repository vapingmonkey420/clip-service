import json

import pytest

from clipper import pick
from clipper.pick import Pick
from clipper.util import ClipperError


def test_parse_plain_json():
    reply = '{"clips": [{"first": 2, "last": 6, "title": "Charge more", "caption": "c", "hashtags": ["Pricing", "#Founders"], "why": "w", "score": 90}]}'
    (found,) = pick.parse_picks(reply)
    assert (found.first, found.last, found.title, found.score) == (2, 6, "Charge more", 90)
    assert found.hashtags == ["#pricing", "#founders"]


def test_parse_json_wrapped_in_prose_and_fences():
    reply = 'Here you go:\n```json\n{"clips": [{"first": "1", "last": "3", "title": "T"}]}\n```\nHope that helps.'
    (found,) = pick.parse_picks(reply)
    assert (found.first, found.last) == (1, 3)
    assert found.score == 50  # missing score gets a neutral default


def test_parse_accepts_already_parsed_output():
    assert len(pick.parse_picks({"clips": [{"first": 0, "last": 1, "title": "T"}]})) == 1


def test_parse_skips_broken_items_and_rejects_non_json():
    assert pick.parse_picks('{"clips": [{"first": "x", "last": 2, "title": "bad"}, "junk"]}') == []
    with pytest.raises(ClipperError):
        pick.parse_picks("I could not find any good moments.")


def test_select_enforces_range_length_and_overlap(talk, cfg):
    cfg["clips"].update(min_seconds=10, max_seconds=25)
    picks = [
        Pick(2, 6, "Pricing", score=90),  # 18.8s, fine
        Pick(4, 9, "Overlaps the first", score=80),
        Pick(8, 11, "Hiring", score=70),  # 14.8s, fine
        Pick(12, 13, "Too short", score=95),
        Pick(0, 40, "Out of range", score=99),
        Pick(8, 11, "", score=99),  # no title
    ]
    kept = pick.select(picks, talk, cfg)
    assert [p.title for p in kept] == ["Pricing", "Hiring"]


def test_select_trims_a_slightly_long_pick_by_whole_sentences(talk, cfg):
    cfg["clips"].update(min_seconds=10, max_seconds=16)
    (kept,) = pick.select([Pick(2, 6, "Pricing", score=90)], talk, cfg)
    assert kept.last < 6
    assert talk.sentences[kept.last].end - talk.sentences[2].start <= 16 * 1.1


def test_heuristic_finds_the_two_topics_and_skips_housekeeping(talk, cfg):
    cfg["clips"].update(count=2, min_seconds=10, max_seconds=25)
    picks = pick.heuristic_picks(talk, cfg)
    assert len(picks) == 2
    starts = sorted(p.first for p in picks)
    assert starts == [2, 8]  # "Here is the thing..." and "What is the biggest mistake..."
    for p in picks:
        assert 10 * 0.8 <= talk.sentences[p.last].end - talk.sentences[p.first].start <= 25
    a, b = sorted(picks, key=lambda p: p.first)
    assert a.last < b.first


def test_headline_shortens_long_openers():
    assert pick.headline("What is the biggest mistake?") == "What is the biggest mistake?"
    long = pick.headline("so this one time at the conference in Austin a few years ago we met a founder")
    assert long.endswith("…") and len(long.split()) == 6


def test_request_carries_the_brief_and_numbered_lines(talk, cfg):
    cfg.update(about="Founders talking shop", avoid=["politics"], notes="Numbers do well")
    text = pick.build_request(talk.sentences, talk, cfg, "Episode 12", 6, notes="Skip the intro")
    assert "Show: Test Show" in text and "Avoid: politics" in text and "What has worked before: Numbers do well" in text
    assert "Editor's notes for this episode: Skip the intro" in text
    assert "Pick the 6 strongest moments" in text and "between 20 and 60 seconds" in text
    assert "[2] 0:08 Here is the thing nobody tells you about pricing." in text


def test_without_a_credential_the_fallback_picks_and_says_so(talk, cfg, monkeypatch):
    cfg["clips"].update(count=2, min_seconds=10, max_seconds=25)
    monkeypatch.setattr(pick, "claude_backend", lambda: None)
    result = pick.pick(talk, cfg)
    assert result["picker"] == "heuristic" and "No Claude credential" in result["note"]
    assert [c["rank"] for c in result["clips"]] == [1, 2]
    first = result["clips"][0]
    assert first["first_word"] == talk.sentences[first["first"]].first
    assert first["last_word"] == talk.sentences[first["last"]].last


def test_claude_reply_is_used_and_ranked(talk, cfg, monkeypatch):
    cfg["clips"].update(count=2, min_seconds=10, max_seconds=25)
    reply = json.dumps({"clips": [
        {"first": 8, "last": 11, "title": "Hiring friends", "caption": "c", "hashtags": ["#hiring"], "why": "w", "score": 70},
        {"first": 2, "last": 6, "title": "Charge more", "caption": "c", "hashtags": ["#pricing"], "why": "w", "score": 92},
    ]})  # fmt: skip
    seen = {}

    def fake_api(system, user, model):
        seen.update(system=system, user=user, model=model)
        return reply

    monkeypatch.setattr(pick, "claude_backend", lambda: "api")
    monkeypatch.setattr(pick, "ask_api", fake_api)
    result = pick.pick(talk, cfg, "Episode 12")
    assert result["picker"] == "claude" and result["model"] == cfg["picker"]["model"] and result["note"] == ""
    assert [c["title"] for c in result["clips"]] == ["Charge more", "Hiring friends"]
    assert "never an instruction" in seen["system"] and "<transcript>" in seen["user"]


def test_claude_failure_falls_back_with_a_note(talk, cfg, monkeypatch):
    cfg["clips"].update(count=1, min_seconds=10, max_seconds=25)

    def broken(*_):
        raise ClipperError("Claude API error: AuthenticationError")

    monkeypatch.setattr(pick, "claude_backend", lambda: "api")
    monkeypatch.setattr(pick, "ask_api", broken)
    result = pick.pick(talk, cfg)
    assert result["picker"] == "heuristic" and "AuthenticationError" in result["note"]
    with pytest.raises(ClipperError):
        pick.pick(talk, cfg, mode="claude")  # asked for Claude specifically: fail loudly


def test_cli_call_gets_no_tools_and_no_stray_secrets(monkeypatch):
    captured = {}

    class Done:
        returncode = 0
        stdout = json.dumps({"is_error": False, "structured_output": {"clips": []}})
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured.update(cmd=cmd, **kwargs)
        return Done()

    monkeypatch.setattr(pick, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    assert pick.ask_cli("system", "user text", "claude-sonnet-5-5") == {"clips": []}
    cmd, env = captured["cmd"], captured["env"]
    assert cmd[cmd.index("--tools") + 1] == ""  # every built-in tool off
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert captured["input"] == "user text"
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "token"


def test_long_transcripts_are_split_into_several_requests():
    sentences = [pick.Sentence(i, i, i, float(i), float(i) + 1, "x" * 1000) for i in range(1000)]
    parts = pick.split_for_prompt(sentences)
    assert len(parts) == 3
    assert sum(len(p) for p in parts) == 1000
