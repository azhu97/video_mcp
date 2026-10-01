from pathlib import Path

import pytest

from clipper.core.segments import (
    Segment,
    clamp,
    clip_filename,
    drop_short,
    format_time,
    merge_overlaps,
    pad,
    parse_time,
    process,
    slugify,
)


@pytest.mark.parametrize("value,expected", [
    (12, 12.0), (12.5, 12.5), ("12.5", 12.5), ("1:05", 65.0), ("01:02:03.250", 3723.25),
    ("0:00:00", 0.0), (" 2:00 ", 120.0),
])
def test_parse_time(value, expected):
    assert parse_time(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["-1", -3, "1:75", "1:61:00", "abc", "", True, float("nan")])
def test_parse_time_rejects(value):
    with pytest.raises(ValueError):
        parse_time(value)


def test_format_time_roundtrip():
    assert format_time(3723.25) == "01:02:03.250"
    assert parse_time(format_time(59.999)) == pytest.approx(59.999)


def test_pad_and_clamp():
    s = pad(Segment(1.0, 5.0), 1.5, 1.5)
    assert (s.start, s.end) == (-0.5, 6.5)
    c = clamp(s, 6.0)
    assert (c.start, c.end) == (0.0, 6.0)
    beyond = clamp(Segment(70, 80), 60)
    assert beyond.duration == 0


def test_merge_overlapping_and_gap():
    segs = [Segment(10, 20, "b"), Segment(0, 5, "a"), Segment(18, 25, "c"), Segment(26, 30, "d")]
    assert merge_overlaps(segs) == [Segment(0, 5, "a"), Segment(10, 25, "b + c"), Segment(26, 30, "d")]
    merged = merge_overlaps(segs, gap_tolerance=1.0)
    assert merged == [Segment(0, 5, "a"), Segment(10, 30, "b + c + d")]


def test_merge_contained_and_duplicate_labels():
    out = merge_overlaps([Segment(0, 10, "x"), Segment(2, 3, "x"), Segment(4, 5, "")])
    assert out == [Segment(0, 10, "x")]


def test_drop_short_and_process():
    assert drop_short([Segment(0, 0.5), Segment(1, 3)], 1.0) == [Segment(1, 3)]
    out = process([Segment(58, 59), Segment(1, 2), Segment(2.5, 3)], 60, 1, 1, merge_gap=0, min_length=0.5)
    assert out == [Segment(0, 4), Segment(57, 60)]


def test_process_without_merge_keeps_overlaps():
    out = process([Segment(0, 5), Segment(3, 8)], 60, merge_gap=None)
    assert len(out) == 2


def test_filenames():
    src = Path("/v/My Match.mkv")
    assert clip_filename(src, 3, Segment(3723.9, 3730, "Goal! Ünïted 2–1")) == "My Match_03_goal-united-2-1_010203.mkv"
    assert clip_filename(src, 1, Segment(5, 6)) == "My Match_01_000005.mkv"
    assert slugify("x" * 100) == "x" * 40
