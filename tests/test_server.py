"""MCP-level tests: in-process for breadth, plus one real stdio round trip."""

import json
import os
import sys
import time

import pytest
from mcp import Client, StdioServerParameters

from clipper.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _data(result):
    assert not result.is_error, result.content[0].text
    return result.structured_content or json.loads(result.content[0].text)


@pytest.fixture
def server():
    return build_server()


async def test_lists_everything(server):
    async with Client(server) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert {"probe_video", "export_clips", "find_key_moments", "transcribe", "search_transcript",
                "get_frames", "contact_sheet", "make_highlight_reel", "export_segments_file",
                "import_segments_file", "start_job", "job_status", "job_result"} <= tools
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert prompts == {"sports_highlights", "lecture_key_points", "stream_best_moments"}


async def test_probe_and_export_flow(server, copy_fixture, media):
    src = copy_fixture("sample.mp4")
    async with Client(server) as c:
        info = _data(await c.call_tool("probe_video", {"path": str(src)}))
        assert info["duration"] == 60 and info["duration_hms"] == "00:01:00.000"
        res = _data(await c.call_tool("export_clips", {
            "path": str(src),
            "segments": [{"start": "0:10", "end": "0:20", "label": "a"}, {"start": 60, "end": 75}],
            "mode": "smart",
        }))
        assert len(res["ok"]) == 1 and len(res["failed"]) == 1
        assert res["ok"][0]["actual"]["start"] == 10
        assert (media / "clips" / "sample_01_a_000010.mp4").exists()


async def test_path_outside_allowed_is_tool_error(server, tmp_path):
    async with Client(server) as c:
        r = await c.call_tool("probe_video", {"path": "/etc/hosts"})
        assert r.is_error and "outside the allowed" in r.content[0].text


async def test_find_frames_and_resources(server, copy_fixture):
    src = copy_fixture("events.mp4")
    async with Client(server) as c:
        found = _data(await c.call_tool("find_key_moments", {"path": str(src), "preset": "sports"}))
        assert len(found["candidates"]) == 6 and "pad_before=4.0" in found["hint"]
        vid = found["video_id"]

        frames = await c.call_tool("get_frames", {"path": str(src), "timestamps": [14.9, "0:15.1"]})
        kinds = [b.type for b in frames.content]
        assert kinds == ["text", "image", "text", "image"]

        sheet = await c.call_tool("contact_sheet", {"path": str(src), "start": 0, "end": 60, "n": 8})
        assert [b.type for b in sheet.content] == ["text", "image"]

        listing = json.loads((await c.read_resource("clipper://videos")).contents[0].text)
        assert listing[0]["video_id"] == vid
        scenes = json.loads((await c.read_resource(f"clipper://analysis/{vid}/scenes")).contents[0].text)
        assert [round(s["time"]) for s in scenes] == [15, 30, 45]

        prompt = await c.get_prompt("sports_highlights", {"path": str(src)})
        assert "preset=\"sports\"" in prompt.messages[0].content.text


async def test_segments_file_tools(server, copy_fixture, media):
    src = copy_fixture("sample.mp4")
    async with Client(server) as c:
        w = _data(await c.call_tool("export_segments_file", {
            "path": str(src), "segments": [{"start": 1, "end": 2, "label": "x"}]}))
        assert w["written"].endswith("sample-proj.llc")
        r = _data(await c.call_tool("import_segments_file", {"file": w["written"]}))
        assert r["segments"] == [{"start": 1.0, "end": 2.0, "label": "x"}]
        assert r["media_file"] == str(src.resolve())


async def test_jobs(server, copy_fixture):
    src = copy_fixture("sample.mp4")
    async with Client(server) as c:
        job = _data(await c.call_tool("start_job", {
            "tool": "export_clips",
            "arguments": {"path": str(src), "segments": [{"start": 1, "end": 3}], "dry_run": True}}))
        for _ in range(200):
            status = _data(await c.call_tool("job_status", {"job_id": job["job_id"]}))
            if status["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        result = _data(await c.call_tool("job_result", {"job_id": job["job_id"]}))
        assert result["result"]["dry_run"] is True
        bad = await c.call_tool("start_job", {"tool": "export_clips", "arguments": {"pth": "x"}})
        assert bad.is_error and "pth" in bad.content[0].text


async def test_stdio_roundtrip(copy_fixture, media):
    """Launch the real entry point as a subprocess, exactly as Claude Desktop would."""
    src = copy_fixture("sample.mp4")
    env = {**os.environ, "ALLOWED_DIRS": str(media)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "clipper.server"], env=env)
    async with Client(params) as c:
        info = _data(await c.call_tool("probe_video", {"path": str(src)}))
        assert info["video"]["codec"] == "h264"
        res = _data(await c.call_tool("export_clips", {
            "path": str(src), "segments": [{"start": 10, "end": 20}, {"start": 60, "end": 75}]}))
        assert len(res["ok"]) == 1


async def test_clock_tool_exports_spans(server, copy_fixture, media):
    src = copy_fixture("clock.mp4")
    async with Client(server) as c:
        crop = await c.call_tool("get_frames", {"path": str(src), "timestamps": [6], "region": "580,320,40,20"})
        assert "crop x=580" in crop.content[0].text and crop.content[1].type == "image"
        res = _data(await c.call_tool("find_clock_running", {
            "path": str(src), "region": "580,320,40,20", "export": True}))
        assert res["count"] == 2 and len(res["spans"]) == 2
        assert res["export"]["clips"] == 2 and not res["export"]["failed"]
        clips = sorted((media / "clips").glob("clock_*clock-*.mp4"))
        assert len(clips) == 2
        bad = await c.call_tool("find_clock_running", {"path": str(src), "region": "nope"})
        assert bad.is_error and "named region" in bad.content[0].text
