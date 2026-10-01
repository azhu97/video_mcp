from pathlib import Path

from clipper.core import ffmpeg

FIXTURES = Path(__file__).parent / "fixtures"


def test_check_finds_binaries():
    ffmpeg_version, ffprobe_version = ffmpeg.check()
    assert ffmpeg_version.startswith("ffmpeg")
    assert ffprobe_version.startswith("ffprobe")


def test_sample_fixture_exists():
    assert (FIXTURES / "sample.mp4").is_file()
