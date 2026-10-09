import pytest

from clipper import reframe
from clipper.reframe import Face, Segment

W, H = 1920, 1080


def samples(duration: float, faces_at):
    """One sample every 1/3 s; faces_at(t) returns the faces visible at time t."""
    count = int(duration * reframe.SAMPLE_FPS)
    return [(i / reframe.SAMPLE_FPS, faces_at(i / reframe.SAMPLE_FPS)) for i in range(count)]


def person(cx: float, cy: float = 0.4, size: float = 0.09) -> Face:
    return Face(cx, cy, size, size * 1.9, 0.95)


def test_cut_detection_finds_spikes_but_not_motion():
    times = [i / 30 for i in range(300)]
    calm = [0.002] * 300
    calm[90] = 0.45  # an obvious cut
    calm[200] = 0.09  # a cut between two similar-looking cameras
    assert reframe.find_cuts(times, calm) == [times[90], times[200]]

    busy = [0.08] * 300  # sustained movement, no cut
    assert reframe.find_cuts(times, busy) == []
    busy[150] = 0.5
    assert reframe.find_cuts(times, busy) == [times[150]]


def test_one_person_gets_a_crop_centred_on_them():
    plan = reframe.plan([], samples(10, lambda t: [person(0.3)]), 10.0, W, H)
    assert [s.kind for s in plan] == ["crop"]
    assert plan[0].start == 0 and plan[0].end == 10.0
    assert plan[0].params["cx"] == pytest.approx(0.3)


def test_two_people_far_apart_are_stacked_left_on_top():
    plan = reframe.plan([], samples(10, lambda t: [person(0.75), person(0.25)]), 10.0, W, H)
    assert [s.kind for s in plan] == ["stack"]
    assert plan[0].params["top"]["cx"] == pytest.approx(0.25)
    assert plan[0].params["bottom"]["cx"] == pytest.approx(0.75)


def test_two_people_close_together_share_one_crop():
    plan = reframe.plan([], samples(10, lambda t: [person(0.45, size=0.05), person(0.57, size=0.05)]), 10.0, W, H)
    assert [s.kind for s in plan] == ["crop"]
    assert plan[0].params["cx"] == pytest.approx(0.51)


def test_no_faces_or_a_crowd_shows_the_whole_picture():
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: []), 10.0, W, H)] == ["fit"]
    crowd = [person(0.2), person(0.4), person(0.6), person(0.8)]
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: crowd), 10.0, W, H)] == ["fit"]


def test_small_background_faces_are_ignored():
    poster = Face(0.9, 0.2, 0.02, 0.04, 0.9)
    plan = reframe.plan([], samples(10, lambda t: [person(0.4), poster]), 10.0, W, H)
    assert [s.kind for s in plan] == ["crop"]
    crew = Face(0.5, 0.3, 0.03, 0.06, 0.9)  # someone far behind two hosts
    plan = reframe.plan([], samples(10, lambda t: [person(0.25), person(0.75), crew]), 10.0, W, H)
    assert [s.kind for s in plan] == ["stack"]


def test_a_room_of_small_similar_faces_is_a_crowd_not_two_hosts():
    """Seen on real classroom footage: two front-row faces just over the size limit, two behind just under."""
    room = [Face(0.16, 0.6, 0.038, 0.087, 0.9), Face(0.67, 0.59, 0.038, 0.086, 0.9),
            Face(0.40, 0.45, 0.028, 0.060, 0.9), Face(0.85, 0.44, 0.027, 0.058, 0.9)]  # fmt: skip
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: room), 10.0, W, H)] == ["fit"]
    distant = [Face(0.3, 0.5, 0.02, 0.05, 0.9), Face(0.7, 0.5, 0.02, 0.05, 0.9)]  # nobody close enough to frame
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: distant), 10.0, W, H)] == ["fit"]


def test_each_shot_is_framed_separately_and_angles_are_reused():
    def faces(t):
        return [person(0.3)] if t < 4 or t >= 8 else [person(0.7)]

    plan = reframe.plan([4.0, 8.0], samples(12, faces), 12.0, W, H)
    assert [(s.start, s.end, s.kind) for s in plan] == [(0.0, 4.0, "crop"), (4.0, 8.0, "crop"), (8.0, 12.0, "crop")]
    assert plan[0].params == plan[2].params  # back on the first camera: identical framing
    assert plan[1].params["cx"] == pytest.approx(0.7)


def test_a_false_cut_inside_one_shot_changes_nothing():
    plan = reframe.plan([5.0], samples(10, lambda t: [person(0.3 + (0.005 if t > 5 else 0))]), 10.0, W, H)
    assert len(plan) == 1 and plan[0].end == 10.0


def test_a_missed_cut_is_caught_from_where_the_face_is():
    plan = reframe.plan([], samples(12, lambda t: [person(0.25)] if t < 6 else [person(0.75)]), 12.0, W, H)
    assert [s.kind for s in plan] == ["crop", "crop"]
    assert plan[0].end == pytest.approx(6.0, abs=0.34)
    assert plan[0].params["cx"] == pytest.approx(0.25) and plan[1].params["cx"] == pytest.approx(0.75)


def test_forced_layouts():
    two = samples(10, lambda t: [person(0.25), person(0.75)])
    assert [s.kind for s in reframe.plan([], two, 10.0, W, H, "fit")] == ["fit"]
    assert [s.kind for s in reframe.plan([], two, 10.0, W, H, "crop")] == ["crop"]
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: []), 10.0, W, H, "crop")] == ["crop"]


def test_crop_box_is_nine_by_sixteen_inside_the_frame():
    for cx in (0.0, 0.1, 0.5, 0.93, 1.0):
        w, h, x, y = reframe.crop_box({"cx": cx}, W, H)
        assert (w, h) == (608, 1080) and y == 0
        assert 0 <= x <= W - w
    assert reframe.crop_box({"cx": 0.5}, W, H)[2] == (W - 608) // 2


def test_crop_tightens_on_a_small_face_but_never_past_double():
    w, h, x, y = reframe.crop_box({"cx": 0.5, "cy": 0.4, "w": 0.04, "h": 0.08}, W, H)
    assert 304 <= w < 608 and w % 2 == 0 and h % 2 == 0
    assert abs(w / h - 9 / 16) < 0.01
    assert 0 <= x <= W - w and 0 <= y <= H - h
    big = reframe.crop_box({"cx": 0.5, "cy": 0.4, "w": 0.12, "h": 0.2}, W, H)
    assert big[:2] == (608, 1080)  # already large in frame: leave it


def test_vertical_and_square_sources():
    w, h, x, y = reframe.crop_box({"cx": 0.5}, 1080, 1920)
    assert (w, h, x, y) == (1080, 1920, 0, 0)
    w, h, x, y = reframe.crop_box({"cx": 0.5}, 1080, 1080)
    assert (w, h) == (608, 1080)


def test_stack_boxes_centre_each_person():
    top = {"cx": 0.25, "cy": 0.4, "w": 0.08, "h": 0.15}
    bottom = {"cx": 0.75, "cy": 0.4, "w": 0.08, "h": 0.15}
    (w1, h1, x1, y1), (w2, h2, x2, y2) = reframe.stack_boxes(top, bottom, W, H)
    assert (w1, h1) == (w2, h2) and abs(w1 / h1 - 1080 / 960) < 0.01
    assert x1 + w1 / 2 == pytest.approx(0.25 * W, abs=2)
    assert x2 + w2 / 2 == pytest.approx(0.75 * W, abs=2)
    for x, y, w, h in ((x1, y1, w1, h1), (x2, y2, w2, h2)):
        assert 0 <= x <= W - w and 0 <= y <= H - h


def test_filters_produce_the_output_size():
    crop = reframe.layout_filter(Segment(0, 1, "crop", {"cx": 0.5}), W, H, "s0")
    assert crop.startswith("crop=608:1080:656:0,scale=1080:1920")
    stack = reframe.layout_filter(
        Segment(0, 1, "stack", {"top": {"cx": 0.25, "cy": 0.4, "w": 0.08}, "bottom": {"cx": 0.75, "cy": 0.4, "w": 0.08}}), W, H, "s1"
    )
    assert "vstack=inputs=2" in stack and stack.count("scale=1080:960") == 2 and "[s1a]" in stack
    fit = reframe.layout_filter(Segment(0, 1, "fit"), W, H, "s2")
    assert "boxblur" in fit and "scale=1080:608" in fit and "overlay" in fit


def test_head_top_for_headline_placement():
    crop = Segment(0, 1, "crop", {"cx": 0.5, "cy": 0.4, "w": 0.12, "h": 0.22})
    top = reframe.head_top(crop, W, H)
    assert 0 < top < 0.4 * 1920
    assert reframe.head_top(Segment(0, 1, "fit"), W, H) is None
    assert reframe.head_top(Segment(0, 1, "crop", {"cx": 0.5}), W, H) is None


def test_someone_right_at_the_camera_gets_a_wider_window():
    """Seen on real webcam-style footage: a 9:16 crop of a face this large is all forehead and chin."""
    close = samples(10, lambda t: [Face(0.48, 0.41, 0.14, 0.34, 0.95)])
    (seg,) = reframe.plan([], close, 10.0, 768, 432)
    assert seg.kind == "fit" and seg.params["w"] == pytest.approx(0.14)
    w, h, x, y = reframe.fit_box(seg.params, 768, 432)
    assert 243 < w < 768 and h == 432  # wider than a 9:16 crop, narrower than the whole frame
    assert 0.25 < 0.14 * 768 / w < 0.31  # the face ends up a bit over a quarter of the width
    assert x + w / 2 == pytest.approx(0.48 * 768, abs=2)
    top, bottom = reframe.fit_band(seg.params, 768, 432)
    assert 0 < top < 500 and bottom == pytest.approx(1920 - top)
    chain = reframe.layout_filter(seg, 768, 432, "s0")
    assert chain.startswith(f"crop={w}:{h}:{x}:{y},split=2") and "boxblur" in chain

    # A normal-sized face is still a plain crop, and a forced crop is respected.
    assert reframe.plan([], samples(10, lambda t: [person(0.5)]), 10.0, W, H)[0].kind == "crop"
    assert reframe.plan([], close, 10.0, 768, 432, "crop")[0].kind == "crop"


def test_fit_without_a_person_shows_everything():
    assert reframe.fit_box({}, W, H) == (W, H, 0, 0)
    top, bottom = reframe.fit_band({}, W, H)
    assert (round(top), round(bottom)) == (656, 1264)


# ------------------------------------------------------------ live streams


def cam(cx: float = 0.84, cy: float = 0.75, size: float = 0.07) -> Face:
    """A streamer's face inside a small camera box in a corner of the gameplay."""
    return Face(cx, cy, size, size * 1.8, 0.95)


def test_a_small_steady_face_on_a_stream_is_a_camera_box():
    frames = [[cam()] for _ in range(30)]
    box = reframe.camera_box(frames, reframe.CAM_MAX_FACE)
    assert box["cx"] == pytest.approx(0.84) and box["cy"] == pytest.approx(0.75)
    # Detection misses now and then, and a poster face in the game art comes and goes. Neither matters.
    patchy = [[cam()] if i % 3 else [] for i in range(30)]
    assert reframe.camera_box(patchy, reframe.CAM_MAX_FACE) is not None
    extra = [[cam(), Face(0.3, 0.3, 0.02, 0.035, 0.9)] for _ in range(30)]
    assert reframe.camera_box(extra, reframe.CAM_MAX_FACE) is not None


def test_what_is_not_a_camera_box():
    limit = reframe.CAM_MAX_FACE
    assert reframe.camera_box([], limit) is None
    assert reframe.camera_box([[] for _ in range(30)], limit) is None
    assert reframe.camera_box([[cam()] if i < 10 else [] for i in range(30)], limit) is None  # there a third of the time
    assert reframe.camera_box([[person(0.5, size=0.2)] for _ in range(30)], limit) is None  # the camera fills the screen
    wandering = [[cam(cx=0.2 + 0.02 * i)] for i in range(30)]
    assert reframe.camera_box(wandering, limit) is None  # a person moving through a scene
    assert reframe.camera_box([[cam(0.2), cam(0.8)] for _ in range(30)], limit) is None  # two people, not one box


def test_a_stream_clip_with_a_camera_box_is_split_and_stays_split_through_cuts():
    cuts = [2.0, 3.1, 7.5]  # game footage is full of sudden changes
    plan = reframe.plan(cuts, samples(12, lambda t: [cam()] if int(t * 3) % 4 else []), 12.0, W, H, "auto", stream=True)
    assert [(s.start, s.end, s.kind) for s in plan] == [(0.0, 12.0, "split")]
    assert plan[0].params["cam"]["cx"] == pytest.approx(0.84)
    # The same footage from a client that is not a stream keeps the old behaviour.
    assert "split" not in [s.kind for s in reframe.plan(cuts, samples(12, lambda t: [cam()]), 12.0, W, H, "auto")]


def test_a_stream_scene_with_the_camera_full_screen_is_framed_like_any_talking_head():
    plan = reframe.plan([], samples(10, lambda t: [person(0.45, size=0.11)]), 10.0, W, H, "auto", stream=True)
    assert [s.kind for s in plan] == ["crop"]
    # Right up at the lens, as a webcam often is: the wider window, exactly as for any other footage.
    close = reframe.plan([], samples(10, lambda t: [person(0.45, size=0.2)]), 10.0, W, H, "auto", stream=True)
    assert [s.kind for s in close] == ["fit"] and close[0].params["w"] == pytest.approx(0.2)
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: []), 10.0, W, H, "auto", stream=True)] == ["fit"]


def test_a_clip_that_goes_from_gameplay_to_full_camera_changes_layout_at_the_cut():
    def faces(t):
        return [cam()] if t < 6 else [person(0.45, size=0.11)]

    plan = reframe.plan([6.0], samples(12, faces), 12.0, W, H, "auto", stream=True)
    assert [(s.start, s.end, s.kind) for s in plan] == [(0.0, 6.0, "split"), (6.0, 12.0, "crop")]


def test_split_can_be_forced_and_falls_back_when_there_is_no_face():
    big = samples(10, lambda t: [person(0.5, size=0.2)])
    assert [s.kind for s in reframe.plan([], big, 10.0, W, H, "split")] == ["split"]
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: []), 10.0, W, H, "split")] == ["fit"]
    assert [s.kind for s in reframe.plan([], samples(10, lambda t: [cam()]), 10.0, W, H, "fit", stream=True)] == ["fit"]


def test_split_windows_are_the_right_shapes_and_inside_the_frame():
    for box in ({"cx": 0.84, "cy": 0.75, "w": 0.07, "h": 0.126}, {"cx": 0.1, "cy": 0.15, "w": 0.05, "h": 0.09}, {"cx": 0.5, "cy": 0.9, "w": 0.1, "h": 0.18}):
        (w1, h1, x1, y1), (w2, h2, x2, y2) = reframe.split_boxes(box, W, H)
        assert abs(w1 / h1 - 1080 / reframe.SPLIT_TOP) < 0.02 and abs(w2 / h2 - 1080 / (1920 - reframe.SPLIT_TOP)) < 0.02
        for x, y, w, h in ((x1, y1, w1, h1), (x2, y2, w2, h2)):
            assert 0 <= x <= W - w and 0 <= y <= H - h and w % 2 == 0 and h % 2 == 0
        assert h2 == H  # the gameplay window always uses the full height
    # The face ends up about a fifth of the camera panel's width, centred where the box allows.
    (w1, h1, x1, y1), _ = reframe.split_boxes({"cx": 0.5, "cy": 0.5, "w": 0.07, "h": 0.126}, W, H)
    assert 0.07 * W / w1 == pytest.approx(reframe.CAM_FACE_SHARE, abs=0.01) and x1 + w1 / 2 == pytest.approx(0.5 * W, abs=2)


def test_the_gameplay_window_stays_near_the_middle():
    centre = (W - reframe.split_boxes({"cx": 0.5, "cy": 0.1, "w": 0.05}, W, H)[1][0]) / 2
    for cx in (0.1, 0.84):  # camera in a left corner, then a right one
        _, (gw, gh, gx, gy) = reframe.split_boxes({"cx": cx, "cy": 0.8, "w": 0.07}, W, H)
        assert abs(gx - centre) <= 0.06 * W + 1  # nudged away from the camera, never far
        assert (gx > centre) == (cx < 0.5) or gx == centre


def test_split_filter_stacks_camera_over_gameplay():
    seg = Segment(0, 1, "split", {"cam": {"cx": 0.84, "cy": 0.75, "w": 0.07, "h": 0.126}})
    chain = reframe.layout_filter(seg, W, H, "s0")
    assert chain.count("crop=") == 2 and "vstack=inputs=2" in chain
    assert f"scale=1080:{reframe.SPLIT_TOP}" in chain and f"scale=1080:{1920 - reframe.SPLIT_TOP}" in chain
    assert reframe.head_top(seg, W, H) is None and reframe.describe([seg]) == "split"


def test_the_same_camera_box_is_framed_identically_each_time_it_returns():
    def faces(t):
        return [cam(cx=0.84 + (0.004 if t > 8 else 0))] if t < 4 or t >= 8 else [person(0.45, size=0.11)]

    plan = reframe.plan([4.0, 8.0], samples(12, faces), 12.0, W, H, "auto", stream=True)
    assert [s.kind for s in plan] == ["split", "crop", "split"] and plan[0].params == plan[2].params


def test_a_camera_box_seen_too_rarely_never_becomes_a_close_up():
    """Zooming a 9:16 crop onto a corner camera would throw the whole game away."""
    rare = samples(12, lambda t: [cam()] if int(t * 3) % 5 < 2 else [])  # found in two frames out of five
    assert [s.kind for s in reframe.plan([], rare, 12.0, W, H, "auto", stream=True)] == ["fit"]
    assert [s.kind for s in reframe.plan([], rare, 12.0, W, H, "auto")] == ["crop"]  # ordinary footage: a person far from the lens

