import importlib.util
import shutil

import pytest

from clipper.core.detect import audio, find_key_moments
from clipper.core.detect import frames as frames_mod
from clipper.core.detect import transcript as tr

HAS_WHISPER = importlib.util.find_spec("faster_whisper") is not None


def _times(result, source):
    return [c["time"] for c in result["candidates"] if c["source"] == source]


def test_scenes_and_audio_on_known_events(copy_fixture):
    src = copy_fixture("events.mp4")
    res = find_key_moments(src, ["scenes", "audio"])
    assert _times(res, "scenes") == pytest.approx([15, 30, 45], abs=0.05)
    assert _times(res, "audio") == pytest.approx([10, 30, 50], abs=0.3)
    assert all(0 <= c["score"] <= 1 for c in res["candidates"])
    assert [c["time"] for c in res["candidates"]] == sorted(c["time"] for c in res["candidates"])


def test_steady_audio_has_no_peaks(copy_fixture):
    res = find_key_moments(copy_fixture("sample.mp4"), ["audio"], sensitivity="high")
    assert res["candidates"] == []


def test_window_and_max_results(copy_fixture):
    src = copy_fixture("events.mp4")
    res = find_key_moments(src, ["scenes"], start=20, end=50)
    assert _times(res, "scenes") == pytest.approx([30, 45], abs=0.05)
    res = find_key_moments(src, ["scenes", "audio"], max_results=2)
    assert res["returned"] == 2 and res["total_found"] == 6


def test_skips_methods_without_streams(copy_fixture):
    res = find_key_moments(copy_fixture("audio_only.m4a"), ["scenes"])
    assert res["skipped"] == {"scenes": "no video stream"}


def test_unknown_method(copy_fixture):
    with pytest.raises(ValueError, match="Unknown method"):
        find_key_moments(copy_fixture("sample.mp4"), ["bogus"])


def test_find_peaks_spacing():
    curve = [[i / 10, -30.0] for i in range(600)]
    for t in (100, 120, 400):  # 10s, 12s (too close), 40s
        curve[t][1] = -5.0
    curve[120][1] = -8.0
    peaks = audio.find_peaks(curve, k=4, min_lu=6)
    assert [p["time"] for p in peaks] == [10.0, 40.0]


def test_frames_and_contact_sheet(copy_fixture):
    src = copy_fixture("sample.mp4")
    shots = frames_mod.frames(src, [1.0, 30.0], width=320)
    assert len(shots) == 2 and all(j[:2] == b"\xff\xd8" for _, j in shots)
    with pytest.raises(ValueError):
        frames_mod.frames(src, [1.0] * 9)
    jpeg, times = frames_mod.contact_sheet(src, 0, 60, n=6)
    assert jpeg[:2] == b"\xff\xd8" and times == [5, 15, 25, 35, 45, 55]


def test_frame_past_end_errors(copy_fixture):
    from clipper.core import ffmpeg

    with pytest.raises(ffmpeg.FFmpegError):
        frames_mod.frames(copy_fixture("sample.mp4"), [500.0])


def test_transcript_search_modes():
    t = {"segments": [
        {"id": 0, "start": 1.0, "end": 3.0, "text": "The request hit a timeout."},
        {"id": 1, "start": 5.0, "end": 7.0, "text": "We added retries to fix the time out."},
        {"id": 2, "start": 9.0, "end": 9.5, "text": "Unrelated."},
    ]}
    assert [h["id"] for h in tr.search(t, "timeout")] == [0]
    assert [h["id"] for h in tr.search(t, "TIME out")] == [1]
    assert [h["id"] for h in tr.search(t, r"retr(y|ies)", mode="regex")] == [1]
    assert {h["id"] for h in tr.search(t, "timeout", mode="fuzzy", fuzzy_threshold=0.8)} == {0, 1}
    with pytest.raises(ValueError):
        tr.search(t, "(", mode="regex")


def test_transcript_paging():
    t = {"language": "en", "segments": [{"id": i, "start": i, "end": i + 1, "text": str(i), "words": []}
                                        for i in range(250)]}
    p = tr.page(t, 0, 100)
    assert len(p["segments"]) == 100 and p["next_offset"] == 100 and "words" not in p["segments"][0]
    assert tr.page(t, 200, 100)["next_offset"] is None


@pytest.mark.transcribe
@pytest.mark.skipif(not HAS_WHISPER, reason="faster-whisper not installed")
def test_transcribe_speech(copy_fixture, monkeypatch):
    if not (tr.Path(__file__).parent / "fixtures" / "speech.mp4").exists():
        pytest.skip("speech fixture needs macOS `say`")
    monkeypatch.setenv("CLIPPER_WHISPER_MODEL", "tiny")
    from clipper import config

    config.get_settings.cache_clear()
    src = copy_fixture("speech.mp4")
    t = tr.transcript(src, words=True)
    text = " ".join(s["text"] for s in t["segments"]).lower()
    assert "timeout" in text and "retries" in text
    hits = tr.search(t, "timeout")
    assert len(hits) == 2 and 4 < hits[0]["start"] < 8
    assert "word_time" in hits[0]
    # Windowed request reuses the cached full transcript.
    sub = tr.transcript(src, start=9, end=18)
    assert all(s["end"] > 9 for s in sub["segments"])
    moments = tr.detect_pauses(src, sensitivity="high")
    assert len(moments) == 2


def test_transcript_cache_is_shared_between_word_modes(copy_fixture, monkeypatch):
    calls = []

    def fake(path, model, start, end, words, progress):
        calls.append(words)
        return {"language": "en", "model": model, "start": 0, "end": None,
                "segments": [{"id": 0, "start": 0, "end": 1, "text": "hi", "words": [[0, 1, "hi"]]}]}

    monkeypatch.setattr(tr, "_run_whisper", fake)
    src = copy_fixture("sample.mp4")
    tr.transcript(src, words=True)
    tr.transcript(src, words=False)
    assert calls == [True]


def test_whisper_missing_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "faster_whisper":
            raise ImportError("nope")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(tr.TranscriptionUnavailable, match="--extra transcribe"):
        tr._model("definitely-not-loaded")


def test_shutil_which_ffmpeg_present():
    assert shutil.which("ffmpeg")


# --- clock -------------------------------------------------------------------------------

def test_clock_spans_ignore_frozen_and_flicker(copy_fixture):
    from clipper.core.detect import clock

    src = copy_fixture("clock.mp4")
    spans = clock.find_running(src, clock.parse_region("580,320,40,20", 640, 360))
    # Ticks during 5-15s and 25-40s; frozen elsewhere; 45-50s flickers every frame (ignored).
    assert [(s["start"], s["end"]) for s in spans] == [
        pytest.approx((5, 15), abs=1.01), pytest.approx((25, 40), abs=1.01)]


def test_clock_region_parsing(monkeypatch):
    from clipper import config
    from clipper.core.detect import clock

    assert clock.parse_region("1001,1010,26,20", 1920, 1080) == clock.Region(1001, 1010, 26, 20)
    assert clock.parse_region([0.5, 0.5, 0.25, 0.1], 1920, 1080) == clock.Region(960, 540, 480, 108)
    with pytest.raises(ValueError, match="not inside"):
        clock.parse_region("1900,1070,40,20", 1920, 1080)
    with pytest.raises(ValueError, match="4 numbers"):
        clock.parse_region("1,2,3", 1920, 1080)
    cfg = config.get_settings().cache_dir / "clock.toml"
    cfg.write_text('[clock_regions]\nshot_clock = "1001,1010,26,20"\n')
    monkeypatch.setenv("CLIPPER_CONFIG", str(cfg))
    config.get_settings.cache_clear()
    assert clock.parse_region("shot_clock", 1920, 1080) == clock.Region(1001, 1010, 26, 20)


def test_running_spans_split_on_gaps():
    from clipper.core.detect.clock import running_spans

    samples = []
    for i in range(int(60 * 6)):
        t = i / 6
        tick = (2 <= t < 10 or 13 <= t < 20) and abs(t - round(t)) < 0.01
        samples.append([t, 40.0 if tick else 1.0])
    spans = running_spans(samples, threshold=8)
    assert [(s["start"], s["end"]) for s in spans] == [(1.0, 10.0), (12.0, 20.0)]


def test_clock_method_in_find_key_moments(copy_fixture):
    src = copy_fixture("clock.mp4")
    res = find_key_moments(src, ["clock"], clock_region="580,320,40,20")
    assert [round(c["time"]) for c in res["candidates"]] == [4, 24]
    with pytest.raises(ValueError, match="clock_region"):
        find_key_moments(src, ["clock"])
