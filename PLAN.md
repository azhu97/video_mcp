# Key-Moment Clipper MCP — Implementation Plan

A local MCP server that lets Claude analyze video files on disk, find key moments, and export them as clips using ffmpeg (lossless stream copy by default, frame-accurate re-encode when needed).

Phases are ordered so that each one produces something usable and testable on its own. Don't start a phase until the previous one's "Done when" criteria pass.

---

## Architecture at a glance

```
Claude Desktop / Claude Code  (MCP client)
        │  stdio
        ▼
clipper MCP server (Python, FastMCP)
  ├── tools/        → thin MCP tool definitions (validation + formatting only)
  ├── core/         → pure Python logic, no MCP imports
  │     ├── ffmpeg.py     (subprocess wrappers: probe, cut, concat)
  │     ├── segments.py   (padding, merging, clamping, naming)
  │     ├── detect/       (scenes.py, audio.py, transcript.py, frames.py)
  │     └── cache.py      (per-video analysis cache)
  └── config.py     → allowed dirs, output dir, defaults
        │
        ▼
ffmpeg / ffprobe  (local binaries)  →  files on local disk
```

**Key rule:** `core/` never imports MCP. Every core function is callable and testable from plain Python. The MCP layer is a thin adapter.

---

## Phase 0 — Environment & project skeleton

**Goal:** A repo that installs cleanly and can find ffmpeg.

Tasks:

- Install Python 3.11+, `uv` (or pip/venv), and ffmpeg (`brew install ffmpeg` / `apt install ffmpeg`).
- Create the project: `uv init clipper-mcp`, add `mcp[cli]`, `pydantic`, `pytest`.
- Set up the directory layout above.
- `config.py`: load settings from env vars or a `config.toml`:
  - `ALLOWED_DIRS` (list of absolute paths the server may read)
  - `OUTPUT_DIR` (default: `<video_dir>/clips/`)
  - `FFMPEG_PATH`, `FFPROBE_PATH` (default: resolved via `shutil.which`)
- Startup check: fail fast with a clear message if ffmpeg/ffprobe aren't found.
- Generate test fixtures with ffmpeg itself (no real footage needed yet):

  ```
  ffmpeg -f lavfi -i testsrc=duration=60:size=1280x720:rate=30 \
         -f lavfi -i sine=frequency=440:duration=60 \
         -g 60 -c:v libx264 -c:a aac tests/fixtures/sample.mp4
  ```

  (`-g 60` → a keyframe every 2s, so keyframe-snapping behavior is predictable.)

**Done when:** `uv run python -c "from clipper.core import ffmpeg; ffmpeg.check()"` passes and fixtures exist.

---

## Phase 1 — Core ffmpeg wrapper (no MCP yet)

**Goal:** Reliable Python functions for probing and cutting.

Tasks:

- `run(cmd, timeout)` helper: `subprocess.run` with list args (never `shell=True`), captured stderr, timeout, and a typed `FFmpegError` that includes the stderr tail.
- `probe(path) -> VideoInfo`: duration, container, video/audio codecs, resolution, fps, bitrate, stream count. Use `ffprobe -v error -print_format json -show_format -show_streams`.
- `keyframes(path) -> list[float]`:

  ```
  ffprobe -v error -select_streams v:0 -skip_frame nokey \
          -show_entries frame=pts_time -of csv=p=0 input.mp4
  ```

  This can be slow on long files, so cache the result (see Phase 3).
- `cut_copy(src, start, end, dst)`: lossless cut.

  ```
  ffmpeg -ss START -i src -t DURATION -c copy -map 0 \
         -avoid_negative_ts make_zero -y dst
  ```

- Return the *actual* start time of the produced clip (nearest keyframe at or before `start`) so callers know about snapping.
- Unit tests against the fixture: probe values, keyframe count ≈ 30, cut duration within ±2s.

**Done when:** You can cut 3 clips from the fixture in a Python REPL, and tests pass.

---

## Phase 2 — Minimal MCP server

**Goal:** Claude can probe a local file and export clips from timestamps you give it.

Tasks:

- `server.py` with FastMCP, stdio transport.
- Tools:
  - `probe_video(path: str)` → JSON summary from `probe()`.
  - `export_clips(path: str, segments: list[{start, end, label?}], output_dir?: str)` → list of output paths + actual start/end per clip.
- **Path safety** (do this now, not later):
  - Resolve to absolute path, `Path.resolve()` to kill `..` and symlink tricks.
  - Reject anything not inside `ALLOWED_DIRS`.
  - Never overwrite the source file; refuse if `dst == src`.
- Accept timestamps as seconds *or* `HH:MM:SS(.ms)` strings; normalize to float seconds.
- Register with Claude Desktop (`claude_desktop_config.json`):

  ```json
  {
    "mcpServers": {
      "clipper": {
        "command": "uv",
        "args": ["--directory", "/abs/path/to/clipper-mcp", "run", "clipper"],
        "env": { "ALLOWED_DIRS": "/Users/you/Videos" }
      }
    }
  }
  ```

- Test with the MCP Inspector (`uv run mcp dev server.py`) before Claude Desktop.

**Done when:** In Claude Desktop, "probe ~/Videos/x.mp4 and cut 0:10–0:20 and 1:00–1:15" produces two playable files.

---

## Phase 3 — Robust batch export

**Goal:** Exporting 20+ clips from a real, long file is reliable and predictable.

Tasks:

- `segments.py`:
  - `pad(seg, before, after)`: default ~1.5s / 1.5s.
  - `clamp(seg, duration)`: keep within `[0, duration]`.
  - `merge_overlaps(segs, gap_tolerance)`: combine segments closer than N seconds.
  - Drop segments shorter than a minimum length.
  - Deterministic filenames: `{stem}_{index:02d}_{label-slug}_{HHMMSS}.{ext}`.
- Keep the source container/extension by default (mp4 → mp4, mkv → mkv).
- Per-clip error isolation: one failing clip doesn't abort the batch; return `{ok: [...], failed: [{segment, error}]}`.
- Write a `manifest.json` in the output dir: source path, settings, each clip's requested vs. actual times.
- `cache.py`: store per-video analysis (keyframes, later scenes/audio/transcript) in `~/.cache/clipper/<sha1 of path+size+mtime>/`. All later detectors use it.
- Timeouts scaled to clip length; sensible concurrency (2–4 parallel ffmpeg processes).
- Add a `dry_run: bool` option to `export_clips` that returns the final, processed segment list without cutting.

**Done when:** A 30-segment export on a 1-hour file completes with a correct manifest, overlapping inputs merge, and a deliberately bad segment is reported without killing the batch.

---

## Phase 4 — Detection v1: scenes + audio (fully local, no AI)

**Goal:** The server proposes candidate key moments on its own.

Tasks:

- `detect/scenes.py`: scene-change detection.
  - Option A: ffmpeg `scdet` filter (`-vf scdet=threshold=10 -f null -`), parse `lavfi.scd.time` / `lavfi.scd.score` from stderr.
  - Option B: PySceneDetect (`ContentDetector`) — easier tuning, extra dependency.
  - Start with one; keep the interface the same so you can swap.
- `detect/audio.py`: loudness peaks.
  - Run `ebur128=metadata=1` (or `astats` over windows) to get momentary loudness over time.
  - Find peaks above `median + k * MAD`, with a minimum spacing between peaks.
  - Good for sports, crowds, streams, gaming.
- Common output type for all detectors:

  ```json
  { "time": 512.4, "score": 0.83, "source": "audio", "note": "loudness spike +9 LU" }
  ```

- Tool: `find_key_moments(path, methods=["scenes","audio"], max_results=50, sensitivity="medium")`.
  - Return candidates sorted by time with scores. Claude decides which to keep.
  - Support `start`/`end` to analyze only part of a long file.
- Results cached per video + parameters.

**Done when:** On real footage, `find_key_moments` returns sensible candidates in reasonable time, and Claude can go from "find the big moments" → candidates → `export_clips` in one conversation.

---

## Phase 5 — Detection v2: transcript-based moments

**Goal:** Find moments by *what is said*.

Tasks:

- Add `faster-whisper` (runs locally; pick model size by hardware — `small` or `medium` is a good default; Apple Silicon may prefer `mlx-whisper`).
- Extract audio first: `ffmpeg -i src -vn -ac 1 -ar 16000 audio.wav`.
- `detect/transcript.py`: produce timestamped segments (and word timestamps if enabled); cache as JSON.
- Tools:
  - `transcribe(path, start?, end?)` → returns a compact transcript (or a cached-file reference + summary if it's huge).
  - `search_transcript(path, query)` → matching segments with timestamps (simple keyword/regex first; fuzzy matching later).
- Paginate long transcripts so you don't flood the model's context.

**Done when:** "Clip every time someone mentions the timeout" works end-to-end on a real file.

---

## Phase 6 — Visual inspection (let Claude look)

**Goal:** Claude can verify or judge candidates visually.

Tasks:

- `detect/frames.py`: extract a downscaled JPEG at given timestamps (`ffmpeg -ss T -i src -frames:v 1 -vf scale=640:-1`).
- Tool: `get_frames(path, timestamps[])` returning MCP image content (cap at ~6–8 frames per call).
- Optional: `contact_sheet(path, start, end, n)` that tiles N frames into one image (`tile` filter) — cheap way to show a whole segment.
- Note: frames returned as tool results are sent to the model; detection itself stays local.

**Done when:** Claude can look at frames around candidates and drop false positives before exporting.

---

## Phase 7 — Frame-accurate cutting

**Goal:** Clips start exactly where requested when it matters.

Tasks:

- Add `mode` to `export_clips`: `"copy"` (default, lossless, keyframe-snapped) | `"reencode"` | `"smart"`.
- `reencode`: `-ss` after input for accuracy, `libx264 -crf 18 -preset veryfast` (or match source codec), copy audio when possible.
- `smart` (most complex — do last):
  1. Re-encode from requested start to the next keyframe, matching source codec params (profile, level, pix_fmt, timebase).
  2. Stream-copy from that keyframe to the end.
  3. Join with the concat demuxer.
  - Verify output with ffprobe; fall back to `reencode` if parameters can't be matched.
- Report the snapping error in `copy` mode so Claude can choose to switch modes.

**Done when:** `reencode` and `smart` clips start within one frame of the request and play correctly in common players.

---

## Phase 8 — Workflow polish

**Goal:** Make common workflows one-step and reviewable.

Tasks:

- `make_highlight_reel(clips[], output)`: concat clips (stream copy if all match, otherwise re-encode), optional fades/crossfades in reencode mode.
- **LosslessCut interop:** `export_segments_file(path, segments, format="llc"|"csv")` so you can open the proposed cuts in LosslessCut, tweak by hand, then export there (or re-import the edited CSV via `import_segments_file`).
- MCP **prompts** for common workflows (e.g. "sports highlights", "lecture key points", "stream best moments") that encode good default methods/thresholds.
- MCP **resources** exposing cached analysis (`clipper://<video>/transcript`, `/scenes`) so the client can read them without re-running tools.
- Progress reporting for long jobs (MCP progress notifications), or a job pattern: `start_job` → `job_status` → `job_result`.
- Sensible defaults per footage type stored in config.

**Done when:** "Find the 10 best moments, show me the frames, export them, and make a reel" works smoothly, and segments round-trip through LosslessCut.

---

## Phase 9 — Hardening, testing, packaging

**Goal:** Something you'd be comfortable sharing or putting on a resume.

Tasks:

- Tests:
  - Unit tests for `segments.py` (padding, merging, clamping edge cases).
  - Integration tests on generated fixtures (multiple containers: mp4, mkv, mov; VFR video; audio-only; no-audio video).
  - MCP-level tests via the Python client SDK calling the server over stdio.
- Edge cases: files with spaces/unicode in paths, variable frame rate, multiple audio tracks, subtitles, very long files (3h+), corrupted files, disk-full errors.
- Structured logging to a file (stdout is reserved for the stdio protocol — **never print to stdout**).
- Packaging: `pyproject.toml` entry point, `uvx clipper-mcp` support, README with setup + Claude Desktop config + example prompts, a short demo GIF.
- CI (GitHub Actions) installing ffmpeg and running the test suite.

**Done when:** A fresh machine can go from `git clone` to working Claude Desktop integration by following the README.

---

## Suggested milestones

| Milestone | Phases | What you can demo |
| --- | --- | --- |
| M1 — "It cuts" | 0–2 | Claude exports clips from timestamps you give it |
| M2 — "It's reliable" | 3 | Large batches, manifests, dry runs |
| M3 — "It finds moments" | 4–5 | Automatic candidates from scenes, audio, and speech |
| M4 — "It sees" | 6 | Claude visually checks candidates before cutting |
| M5 — "It's precise" | 7 | Frame-accurate and smart cuts |
| M6 — "It's a product" | 8–9 | Highlight reels, LosslessCut round-trip, packaged + tested |

## Gotchas to keep in mind throughout

- **Stdout is the protocol channel** in stdio MCP servers. Log to stderr or a file only.
- **Keyframe snapping** is inherent to lossless cuts — always report requested vs. actual times.
- **Don't return huge payloads** (full transcripts, thousands of scene cuts) as tool results; summarize, paginate, or cache and reference.
- **Validate every path** against the allowed directories before touching it.
- **Cache aggressively** — keyframe scans, scene detection, and transcription are the slow parts and rarely need re-running.
