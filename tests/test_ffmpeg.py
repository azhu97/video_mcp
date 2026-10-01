import re
from pathlib import Path

import pytest

from clipper.core import ffmpeg

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample.mp4"


def test_check_finds_binaries():
    ffmpeg_version, ffprobe_version = ffmpeg.check()
    assert ffmpeg_version.startswith("ffmpeg")
    assert ffprobe_version.startswith("ffprobe")


def test_check_reports_missing(monkeypatch):
    from clipper import config

    monkeypatch.setenv("FFMPEG_PATH", "/nonexistent/ffmpeg")
    config.get_settings.cache_clear()
    with pytest.raises(ffmpeg.FFmpegNotFoundError, match="could not be run"):
        ffmpeg.check()


def test_probe():
    info = ffmpeg.probe(SAMPLE)
    assert info.duration == pytest.approx(60, abs=0.1)
    assert info.video.codec == "h264"
    assert (info.video.width, info.video.height) == (1280, 720)
    assert info.video.fps == 30
    assert info.audio.codec == "aac"
    assert not info.is_vfr


def test_probe_vfr_and_audio_only():
    assert ffmpeg.probe(FIXTURES / "vfr.mp4").is_vfr
    a = ffmpeg.probe(FIXTURES / "audio_only.m4a")
    assert a.video is None and a.audio is not None


def test_probe_corrupted_has_clear_error():
    with pytest.raises(ffmpeg.FFmpegError) as e:
        ffmpeg.probe(FIXTURES / "corrupted.mp4")
    assert "corrupted" in e.value.reason or "moov" in str(e.value)


def test_disk_full_reason():
    err = ffmpeg.FFmpegError(["ffmpeg"], 1, "av_interleaved_write_frame(): No space left on device")
    assert err.reason.startswith("disk full")


def test_keyframes_every_two_seconds():
    kfs = ffmpeg.keyframes(SAMPLE)
    assert len(kfs) == 30
    assert kfs[:3] == pytest.approx([0, 2, 4])
    assert ffmpeg.keyframe_at_or_before(kfs, 10.5) == 10
    assert ffmpeg.keyframe_at_or_before(kfs, 10.0) == 10
    assert ffmpeg.keyframe_at_or_after(kfs, 10.5) == 12
    assert ffmpeg.keyframe_at_or_after(kfs, 59.5) is None


def test_cut_copy_snaps_to_keyframe(tmp_path):
    kfs = ffmpeg.keyframes(SAMPLE)
    start, end = ffmpeg.cut_copy(SAMPLE, 10.5, 15.0, tmp_path / "c.mp4", kfs=kfs)
    assert start == 10.0
    assert end == pytest.approx(15.0, abs=0.25)
    assert ffmpeg.probe(tmp_path / "c.mp4").duration == pytest.approx(5.0, abs=0.25)


def _best_matching_source_frame(clip: Path, src: Path, around: float, fps: float = 30) -> float:
    """Which source frame near ``around`` does the clip's first frame look like? (max PSNR)"""
    center = round(around * fps)
    best_n, best = None, -1.0
    for n in range(center - 2, center + 3):
        res = ffmpeg.ffmpeg(
            "-i", str(clip), "-i", str(src), "-lavfi",
            f"[0:v]trim=end_frame=1,setpts=PTS-STARTPTS[a];"
            f"[1:v]select=eq(n\\,{n}),setpts=PTS-STARTPTS[b];[a][b]psnr",
            "-f", "null", "-", loglevel="info",
        )
        m = re.search(r"average:(\S+)", res.stderr)
        psnr = float("inf") if m.group(1) == "inf" else float(m.group(1))
        if psnr > best:
            best_n, best = n, psnr
    return best_n / fps


@pytest.mark.parametrize("mode", ["reencode", "smart"])
def test_frame_accurate_modes(tmp_path, mode):
    cut = ffmpeg.cut_reencode if mode == "reencode" else ffmpeg.cut_smart
    dst = tmp_path / f"{mode}.mp4"
    start, end = cut(SAMPLE, 10.5, 15.2, dst)
    assert (start, end) == (10.5, 15.2)
    out = ffmpeg.probe(dst)
    assert out.duration == pytest.approx(4.7, abs=1 / 30 + 0.01)
    # First frame of the clip must be the source frame at 10.5s, not a neighbor.
    assert _best_matching_source_frame(dst, SAMPLE, 10.5) == pytest.approx(10.5)
    if mode == "smart":
        ffmpeg.verify_decodes(dst)


def test_smart_on_keyframe_is_pure_copy(tmp_path):
    start, _ = ffmpeg.cut_smart(SAMPLE, 10.0, 14.0, tmp_path / "k.mp4")
    assert start == 10.0


def test_smart_refuses_vfr(tmp_path):
    with pytest.raises(ffmpeg.SmartCutUnsupported, match="variable frame rate"):
        ffmpeg.cut_smart(FIXTURES / "vfr.mp4", 3.1, 9.0, tmp_path / "v.mp4")
