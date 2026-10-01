import time
from pathlib import Path

import pytest

from clipper.core import ffmpeg, segfile
from clipper.core.export import ExportSettings, export_clips
from clipper.core.jobs import JobManager
from clipper.core.reel import make_highlight_reel
from clipper.core.segments import Segment


@pytest.fixture
def three_clips(copy_fixture, media):
    src = copy_fixture("sample.mp4")
    res = export_clips(src, [Segment(2, 5), Segment(20, 23), Segment(40, 43)], media / "clips",
                       ExportSettings(mode="reencode"))
    return [Path(c["path"]) for c in res["ok"]]


def test_reel_stream_copy(three_clips, media):
    out = media / "reel.mp4"
    res = make_highlight_reel(three_clips, out)
    assert res["method"] == "copy"
    assert ffmpeg.probe(out).duration == pytest.approx(9.0, abs=0.2)


@pytest.mark.parametrize("transition,expected", [("fade", 9.0), ("crossfade", 9.0 - 2 * 0.5)])
def test_reel_transitions(three_clips, media, transition, expected):
    out = media / f"reel-{transition}.mp4"
    res = make_highlight_reel(three_clips, out, transition=transition, transition_duration=0.5)
    assert res["method"] == "reencode"
    assert ffmpeg.probe(out).duration == pytest.approx(expected, abs=0.2)


def test_reel_mixed_formats_reencodes(copy_fixture, media, three_clips):
    mixed = [three_clips[0], copy_fixture("vfr.mp4"), copy_fixture("no_audio.mp4")]
    out = media / "mixed.mp4"
    res = make_highlight_reel(mixed, out)
    info = ffmpeg.probe(out)
    assert res["method"] == "reencode" and "note" in res
    assert (info.video.width, info.video.height) == (1280, 720)
    assert info.audio is not None


def test_llc_and_csv_roundtrip(tmp_path):
    media = tmp_path / "game.mp4"
    segs = [Segment(1.5, 4.25, "goal"), Segment(10, 12, "")]
    for fmt in ("llc", "csv"):
        dst = segfile.default_path(media, fmt)
        segfile.write(dst, media, segs, fmt)
        back, name = segfile.read(dst)
        assert back == segs
        if fmt == "llc":
            assert dst.name == "game-proj.llc" and name == "game.mp4"


def test_reads_losslesscut_json5_and_hand_edited_csv(tmp_path):
    llc = tmp_path / "x-proj.llc"
    llc.write_text("""{
  // written by LosslessCut
  version: 1,
  mediaFileName: 'x.mp4',
  cutSegments: [
    { start: 1.5, end: 3, name: 'it\\'s a "goal"', },
    { start: 7, end: undefined_marker_skipped_below, },
  ],
}""".replace("undefined_marker_skipped_below", "null"))
    segs, media = segfile.read(llc)
    assert media == "x.mp4"
    assert segs == [Segment(1.5, 3.0, 'it\'s a "goal"')]

    csv = tmp_path / "edited.csv"
    csv.write_text("start,end,name\n00:00:01.5,00:00:03,a\n10,12,\n\n20,,open\n")
    assert segfile.read(csv)[0] == [Segment(1.5, 3, "a"), Segment(10, 12, "")]


def test_job_manager():
    jm = JobManager(workers=1)

    def work(progress):
        for i in range(3):
            progress(i + 1, 3, f"step {i}")
        return {"answer": 42}

    job = jm.start("demo", work)
    for _ in range(100):
        if jm.get(job.id).status == "done":
            break
        time.sleep(0.02)
    assert jm.get(job.id).result == {"answer": 42}
    assert jm.get(job.id).view()["progress"] == 1.0

    bad = jm.start("demo", lambda p: 1 / 0)
    for _ in range(100):
        if jm.get(bad.id).finished:
            break
        time.sleep(0.02)
    assert jm.get(bad.id).status == "failed" and "ZeroDivisionError" in jm.get(bad.id).error
    with pytest.raises(KeyError):
        jm.get("nope")
