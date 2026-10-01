"""Long-file tests (Phase 3 / Phase 9 acceptance). Run with: pytest -m slow"""

import json
import random
import subprocess
import time

import pytest

from clipper.core import ffmpeg
from clipper.core.detect import find_key_moments
from clipper.core.export import ExportSettings, export_clips
from clipper.core.segments import Segment

pytestmark = pytest.mark.slow


def _make_long(path, seconds, size, rate):
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate={rate}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", str(rate * 4), "-c:a", "aac", "-b:a", "48k", str(path),
    ], check=True)


@pytest.fixture(scope="module")
def hour_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("long") / "one hour.mp4"
    _make_long(p, 3600, "320x180", 15)
    return p


def test_thirty_segments_on_one_hour_file(hour_file, media):
    src = hour_file
    rng = random.Random(7)
    segs = [Segment(t, t + rng.uniform(5, 20), f"moment {i}")
            for i, t in enumerate(sorted(rng.uniform(0, 3500) for _ in range(28)))]
    segs += [Segment(segs[3].start + 2, segs[3].end + 5, "overlaps 3"), Segment(4000, 4010, "bad")]
    t0 = time.monotonic()
    res = export_clips(src, segs, media / "clips", ExportSettings(pad_before=1.5, pad_after=1.5, merge_gap=1))
    elapsed = time.monotonic() - t0
    assert [f["segment"]["label"] for f in res["failed"]] == ["bad"]
    assert len(res["ok"]) <= 28  # at least the overlap merged
    assert any("overlaps 3" in c["label"] for c in res["ok"])
    manifest = json.loads((media / "clips" / "one hour.manifest.json").read_text())
    assert len(manifest["clips"]) == len(res["ok"])
    for c in manifest["clips"]:
        assert c["actual"]["start"] <= c["requested"]["start"] + 0.01
        assert ffmpeg.probe(c["path"]).duration > 5
    assert elapsed < 120, f"export took {elapsed:.0f}s"


def test_detection_on_one_hour_file_is_cached(hour_file, media):
    src = hour_file
    t0 = time.monotonic()
    find_key_moments(src, ["scenes", "audio"], start=600, end=1200)
    first = time.monotonic() - t0
    t0 = time.monotonic()
    find_key_moments(src, ["scenes", "audio"], start=600, end=1200)
    assert time.monotonic() - t0 < first / 5


def test_three_hour_file(media):
    src = media / "three hours.mp4"
    _make_long(src, 3 * 3600, "160x90", 5)
    info = ffmpeg.probe(src)
    assert info.duration == pytest.approx(3 * 3600, abs=1)
    res = export_clips(src, [Segment(10_000, 10_010)], media, ExportSettings(mode="smart"))
    assert not res["failed"]
    clip = res["ok"][0]
    assert clip["actual"]["start"] == 10_000
    assert ffmpeg.probe(clip["path"]).duration == pytest.approx(10, abs=0.3)
