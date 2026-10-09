from clipper import render
from clipper.reframe import Segment

INFO = {"width": 1920, "height": 1080}
CROP_A = Segment(0.0, 5.0, "crop", {"cx": 0.3})
CROP_B = Segment(5.0, 10.0, "crop", {"cx": 0.7})


def test_one_shot_one_part_is_a_single_piece():
    pieces = render.cut_segments([Segment(0.0, 10.0, "fit")], [(0, 300)])
    assert [(a, b, seg.kind) for a, b, seg in pieces] == [(0, 300, "fit")]


def test_pieces_follow_both_camera_cuts_and_trimmed_pauses():
    # Two shots (cut at 5s); a pause removed between frames 120 and 180.
    pieces = render.cut_segments([CROP_A, CROP_B], [(0, 120), (180, 300)])
    assert [(a, b) for a, b, _ in pieces] == [(0, 120), (180, 300)]
    assert pieces[0][2] is CROP_A and pieces[1][2] is CROP_B

    # Pause removed inside the first shot: its two halves stay separate pieces, then the cut.
    pieces = render.cut_segments([CROP_A, CROP_B], [(0, 60), (90, 300)])
    assert [(a, b, seg.params["cx"]) for a, b, seg in pieces] == [(0, 60, 0.3), (90, 150, 0.3), (150, 300, 0.7)]


def test_every_kept_frame_is_covered_exactly_once():
    segments = [Segment(0.0, 3.34, "crop", {"cx": 0.3}), Segment(3.34, 7.01, "fit"), Segment(7.01, 12.0, "crop", {"cx": 0.6})]
    frames = [(0, 95), (110, 200), (230, 360)]
    pieces = render.cut_segments(segments, frames)
    covered = sorted((a, b) for a, b, _ in pieces)
    assert sum(b - a for a, b in covered) == sum(b - a for a, b in frames)
    for (_, end), (start, _) in zip(covered, covered[1:]):
        assert end <= start  # no overlap


def test_video_chain_counts_frames_not_seconds():
    chain = render.video_chain([(0, 120, CROP_A), (180, 300, CROP_B)], 1920, 1080)
    assert chain.startswith("[0:V:0]fps=30:start_time=0") and "split=2[v0][v1]" in chain
    assert "[v0]trim=start_frame=0:end_frame=120," in chain
    assert "[v1]trim=start_frame=180:end_frame=300," in chain
    assert chain.endswith("[p0][p1]concat=n=2:v=1:a=0[vcat]")

    single = render.video_chain([(0, 300, CROP_A)], 1920, 1080)
    assert "split" not in single and "concat" not in single and single.endswith("[vcat]")


def test_audio_chain_matches_the_same_frames():
    chain = render.audio_chain([(0, 120), (180, 300)], "volume=0.00dB")
    assert chain.startswith("[0:a:0]") and "asplit=2[a0][a1]" in chain
    assert "[a0]atrim=start=0.00000:end=4.00000" in chain and "[a1]atrim=start=6.00000:end=10.00000" in chain
    assert "concat=n=2:v=0:a=1[acat]" in chain and chain.endswith("[aout]")
    assert chain.count("afade=t=out") == 1 and chain.count("afade=t=in") == 1  # only at the join

    single = render.audio_chain([(0, 300)], "volume=0.00dB", wave=True)
    assert "asplit=2[aout][aw]" in single and "afade" not in single


def test_text_positions_by_layout():
    stack = Segment(0, 5, "stack", {"top": {"cx": 0.25, "cy": 0.4, "w": 0.08, "h": 0.15}, "bottom": {"cx": 0.75, "cy": 0.4, "w": 0.08, "h": 0.15}})
    caption_y, _, _ = render.text_positions([(0, 150, stack)], INFO)
    assert caption_y == 960  # on the seam between the two people

    caption_y, title_y, floor = render.text_positions([(0, 150, Segment(0, 5, "fit"))], INFO)
    band_top, band_bottom = (1920 - 608) / 2, (1920 + 608) / 2
    assert caption_y > band_bottom and title_y < band_top and floor < band_top

    face = Segment(0, 5, "crop", {"cx": 0.5, "cy": 0.4, "w": 0.12, "h": 0.22})
    caption_y, title_y, floor = render.text_positions([(0, 150, face)], INFO)
    assert caption_y == int(1920 * 0.66) and floor is not None and floor < 0.4 * 1920


def test_audiogram_chain_uses_cover_art_when_there_is_some():
    plain = render.audiogram_chain(12.0, False, "#FFD60A")
    assert "color=c=" in plain and "showwaves" in plain and "0xFFD60A" in plain and "[1:v]" not in plain
    with_art = render.audiogram_chain(12.0, True)
    assert "[1:v]" in with_art and "boxblur" in with_art


def test_text_sits_around_the_seam_in_the_stream_layout():
    from clipper import reframe

    seg = reframe.Segment(0, 10, "split", {"cam": {"cx": 0.84, "cy": 0.75, "w": 0.07, "h": 0.126}})
    caption_y, title_y, floor = render.text_positions([(0, 300, seg)], {"width": 1920, "height": 1080})
    assert title_y < reframe.SPLIT_TOP < caption_y and floor is None
    assert caption_y - reframe.SPLIT_TOP < 200 and reframe.SPLIT_TOP - title_y < 150  # both close to the seam, clear of the face
