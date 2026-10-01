import json

import pytest

from clipper.core import cache, ffmpeg
from clipper.core.export import ExportSettings, cached_keyframes, export_clips
from clipper.core.paths import PathNotAllowedError, resolve_input, resolve_output_dir
from clipper.core.segments import Segment


def test_export_batch_with_bad_segments(copy_fixture, media):
    src = copy_fixture("sample.mp4")
    out = media / "clips"
    out.mkdir()
    segs = [Segment(10.5, 15, "first"), Segment(14, 20, "overlap"), Segment(30, 35),
            Segment(80, 90, "past end"), Segment(5, 4, "backwards")]
    res = export_clips(src, segs, out, ExportSettings(mode="copy"))
    assert len(res["ok"]) == 2  # first+overlap merged, 30-35
    assert res["ok"][0]["label"] == "first + overlap"
    assert {f["segment"]["label"] for f in res["failed"]} == {"past end", "backwards"}
    for clip in res["ok"]:
        assert ffmpeg.probe(clip["path"]).duration > 4
    manifest = json.loads((out / "sample.manifest.json").read_text())
    assert manifest["source"] == str(src)
    assert manifest["clips"][0]["requested"]["start"] == 10.5
    assert manifest["clips"][0]["actual"]["start"] == 10.0
    assert "snapped" in manifest["clips"][0]["note"]


def test_one_failing_clip_does_not_kill_batch(copy_fixture, media, monkeypatch):
    src = copy_fixture("sample.mp4")
    real = ffmpeg.cut_copy

    def flaky(src_, start, end, dst, **kw):
        if start == 20:
            raise ffmpeg.FFmpegError(["ffmpeg"], 1, "boom")
        return real(src_, start, end, dst, **kw)

    monkeypatch.setattr(ffmpeg, "cut_copy", flaky)
    res = export_clips(src, [Segment(2, 4), Segment(20, 22), Segment(40, 42)], media)
    assert len(res["ok"]) == 2
    assert len(res["failed"]) == 1 and res["failed"][0]["segment"]["start"] == 20
    assert not any(p.name.endswith("_000020.mp4") for p in media.iterdir())


def test_dry_run_padding_and_names(copy_fixture, media):
    src = copy_fixture("sample.mp4")
    res = export_clips(src, [Segment(1, 2, "Goal!"), Segment(58, 59)], media,
                       ExportSettings(pad_before=1.5, pad_after=1.5), dry_run=True)
    segs = res["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [(0.0, 3.5), (56.5, 60.0)]
    assert segs[0]["output"].endswith("sample_01_goal_000000.mp4")
    assert not list(media.glob("*_01_*"))


def test_smart_mode_falls_back_for_vfr(copy_fixture, media):
    src = copy_fixture("vfr.mp4")
    res = export_clips(src, [Segment(3.1, 9)], media, ExportSettings(mode="smart"))
    clip = res["ok"][0]
    assert clip["mode"] == "reencode" and "variable frame rate" in clip["note"]


@pytest.mark.parametrize("name", ["sample.mkv", "sample.mov", "no_audio.mp4", "audio_only.m4a",
                                  "multi.mkv", "spaced ünïcode name.mp4", "vfr.mp4"])
@pytest.mark.parametrize("mode", ["copy", "reencode", "smart"])
def test_containers_and_shapes(copy_fixture, media, name, mode):
    src = copy_fixture(name)
    res = export_clips(src, [Segment(4.5, 8.5, "x")], media, ExportSettings(mode=mode))
    assert not res["failed"], res["failed"]
    clip = res["ok"][0]
    out = ffmpeg.probe(clip["path"])
    assert clip["path"].endswith(src.suffix)
    # Copy mode snaps back to a keyframe; the reported actual start says how far.
    assert out.duration == pytest.approx(8.5 - clip["actual"]["start"], abs=0.25)
    if mode != "copy":
        assert clip["actual"]["start"] == 4.5
    assert (out.video is None) == (ffmpeg.probe(src).video is None)


def test_multi_track_copy_keeps_all_streams(copy_fixture, media):
    src = copy_fixture("multi.mkv")
    res = export_clips(src, [Segment(0, 12)], media)
    types = [s.type for s in ffmpeg.probe(res["ok"][0]["path"]).streams]
    assert types.count("audio") == 2 and "subtitle" in types


def test_corrupted_file_reports_error(copy_fixture, media):
    src = copy_fixture("corrupted.mp4")
    with pytest.raises(ffmpeg.FFmpegError):
        export_clips(src, [Segment(1, 2)], media)


def test_keyframes_are_cached(copy_fixture, monkeypatch):
    src = copy_fixture("sample.mp4")
    first = cached_keyframes(src)
    monkeypatch.setattr(ffmpeg, "keyframes", lambda p: pytest.fail("should be cached"))
    assert cached_keyframes(src) == first
    assert "keyframes" in cache.list_videos()[0]["analyses"]


def test_cache_invalidates_when_file_changes(copy_fixture):
    src = copy_fixture("sample.mp4")
    before = cache.video_id(src)
    src.write_bytes(src.read_bytes() + b"\0")
    assert cache.video_id(src) != before


def test_path_safety(media, tmp_path, copy_fixture):
    src = copy_fixture("sample.mp4")
    assert resolve_input(str(src)) == src.resolve()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(PathNotAllowedError):
        resolve_input(str(outside))
    with pytest.raises(PathNotAllowedError):
        resolve_input(str(media / ".." / outside.parent.name / "outside.mp4"))
    link = media / "link.mp4"
    link.symlink_to(outside)
    with pytest.raises(PathNotAllowedError):
        resolve_input(str(link))
    with pytest.raises(PathNotAllowedError):
        resolve_input("relative.mp4")
    with pytest.raises(PathNotAllowedError):
        resolve_output_dir(src, str(tmp_path / "elsewhere"))
    assert resolve_output_dir(src, None) == (media / "clips").resolve()


def test_never_overwrites_source(copy_fixture, media):
    from clipper.core.paths import ensure_not_source

    src = copy_fixture("sample.mp4")
    with pytest.raises(PathNotAllowedError):
        ensure_not_source(media / "sample.mp4", src)
