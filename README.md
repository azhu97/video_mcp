# clipper-mcp

A local [MCP](https://modelcontextprotocol.io) server that lets Claude find key moments in video
files on your disk and export them as clips with ffmpeg. Everything runs locally: scene and
loudness detection use ffmpeg filters, transcription uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper). The only thing sent to the model is
what the tools return (text, plus frames when Claude asks to look).

- **Lossless by default**: stream-copy cuts, with the keyframe snap reported per clip.
- **Frame-accurate when needed**: `reencode`, or `smart` (re-encodes only up to the first keyframe
  and stream-copies the rest).
- **Finds moments**: scene changes, loudness spikes, pauses in speech, and transcript search.
- **Lets Claude look**: still frames and contact sheets come back as images.
- **Workflow extras**: batch export with a manifest, highlight reels with fades or crossfades,
  LosslessCut round-trips, background jobs for long files.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- ffmpeg and ffprobe on `PATH` (`brew install ffmpeg` / `apt install ffmpeg`)

## Install

```sh
git clone <this repo> clipper-mcp && cd clipper-mcp
uv sync                      # core
uv sync --extra transcribe   # + local speech-to-text (faster-whisper)
```

Or run without cloning: `uvx --from 'clipper-mcp[transcribe]' clipper-mcp` (once published), or
`uvx --from /path/to/clipper-mcp clipper-mcp`.

> **Project inside an iCloud-synced folder (`~/Documents`, `~/Desktop`)?** iCloud marks files in
> `.venv` as hidden, and Python then skips the `.pth` file that makes the package importable
> (`ModuleNotFoundError: No module named 'clipper'`). Keep the venv out of sync:
> `mv .venv .venv.nosync && ln -s .venv.nosync .venv`, or set `UV_PROJECT_ENVIRONMENT` to a
> path outside the synced folder.

## Connect it to Claude

**Claude Desktop**: add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows), then restart Claude:

```json
{
  "mcpServers": {
    "clipper": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/clipper-mcp", "run", "clipper"],
      "env": { "ALLOWED_DIRS": "/Users/you/Movies" }
    }
  }
}
```

If Claude Desktop can't find `uv`, use its absolute path (`which uv`).

**Claude Code**:

```sh
claude mcp add clipper -e ALLOWED_DIRS=$HOME/Movies -- uv --directory /abs/path/to/clipper-mcp run clipper
```

**MCP Inspector** (debugging): `npx @modelcontextprotocol/inspector uv --directory . run clipper`

## Configuration

Environment variables override `~/.config/clipper/config.toml` (or the file named by
`CLIPPER_CONFIG`), which overrides built-in defaults.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_DIRS` | *(none: every path is refused)* | Folders the server may read and write, separated by `:` or `,` |
| `OUTPUT_DIR` | `<video dir>/clips` | Where clips go when a call doesn't say |
| `FFMPEG_PATH`, `FFPROBE_PATH` | found on `PATH` | ffmpeg binaries |
| `CLIPPER_CACHE` | `~/.cache/clipper` | Analysis cache (keyframes, scenes, loudness, transcripts) |
| `CLIPPER_LOG` | `<cache>/clipper.log` | Log file (stdout is reserved for the MCP protocol) |
| `CLIPPER_MAX_PARALLEL` | `3` | Concurrent ffmpeg processes during batch export |
| `CLIPPER_WHISPER_MODEL` | `small` | faster-whisper model (`tiny`, `base`, `small`, `medium`, `large-v3`) |

`config.toml` uses the same keys in lowercase and can override footage presets:

```toml
allowed_dirs = ["/Users/you/Movies", "/Volumes/Footage"]
whisper_model = "medium"

[clock_regions]               # named regions for find_clock_running
synergy_shot_clock = "1001,1010,26,20"

[presets.sports]
pad_before = 6.0
sensitivity = "high"
```

## Tools

| Tool | What it does |
| --- | --- |
| `probe_video` | Duration, codecs, resolution, fps, stream counts |
| `find_key_moments` | Scored candidates from `scenes`, `audio`, `transcript`, `clock`; supports `start`/`end` windows and `preset` |
| `find_clock_running` | Spans where an on-screen shot/game clock is ticking (live play); can export each span as a clip |
| `transcribe` | Paginated, timestamped transcript (cached) |
| `search_transcript` | `keyword`, `regex` or `fuzzy` search; returns timestamps |
| `get_frames` | Up to 8 JPEG frames at given times; `region` crops and enlarges part of the frame |
| `contact_sheet` | N frames from a span, tiled into one image |
| `export_clips` | Batch cut with padding, merging, `mode` = `copy` / `reencode` / `smart`, `dry_run`, manifest |
| `make_highlight_reel` | Join clips; stream copy when possible, optional `fade` / `crossfade` |
| `export_segments_file` / `import_segments_file` | LosslessCut `.llc` project or CSV, in both directions |
| `start_job` / `job_status` / `job_result` | Run any long tool in the background |

**Prompts**: `sports_highlights`, `lecture_key_points`, `stream_best_moments`.
**Resources**: `clipper://videos` (cached videos) and `clipper://analysis/{video_id}/{transcript|scenes|audio|keyframes|probe}`.

Times can be given as seconds (`75.5`) or `HH:MM:SS.ms` / `MM:SS`.

## Example prompts

- "Probe ~/Movies/match.mp4 and cut 0:10–0:20 and 1:00–1:15."
- "Find the 10 biggest moments in ~/Movies/match.mp4, show me frames, export them with smart cuts, then make a reel with crossfades."
- "Clip every time someone mentions the timeout in ~/Movies/standup.mov."
- "Propose highlight segments for ~/Movies/stream.mkv as a LosslessCut project so I can tweak them."

## Clock detection (sports broadcasts)

`find_clock_running` finds the stretches where a scoreboard clock is ticking, without OCR. A
running clock changes its digits once per second and a stopped one is frozen, so the tool crops
the clock's screen region, samples it 6 times a second, and groups the once-per-second changes
into spans. Seconds where the region changes constantly (scoreboard hidden behind video) are
ignored.

- **Pick the region:** Claude can find it itself: `get_frames` on a live-play moment, then
  `get_frames(region="x,y,w,h")` to check that the crop tightly contains the clock digits. Save
  regions you reuse under `[clock_regions]` in `config.toml` and pass the name instead.
- **Shot clock vs game clock:** the shot-clock region gives "shot clock ticking" spans, which
  split at made baskets, resets and violations, so roughly one per possession. The game-clock
  region gives continuous live play. A game clock showing tenths of a second in the final minute
  changes every sample and reads as "hidden", so that stretch is missed.
- **Pregame/halftime countdowns** on the same scoreboard also tick: pass `start=` (tip-off).
- **Speed:** about 60x real time (a 3-hour game takes ~3 min); results are cached. For an uncached
  full game use `start_job(tool="find_clock_running", ...)` if your client times out long calls.

Example: *"Export every stretch where the shot clock is ticking in ~/Movies/game.mp4 as separate
clips (region synergy_shot_clock, start at tip-off)."*

## How cutting works

| Mode | Speed | Quality | Start accuracy |
| --- | --- | --- | --- |
| `copy` (default) | fastest | lossless | snaps back to the previous keyframe; each clip reports `start_snap_seconds` |
| `smart` | fast | lossless after the first keyframe | exact frame (H.264/HEVC, constant frame rate); otherwise falls back to `reencode` and says so |
| `reencode` | slowest | high quality (x264 CRF 18) | exact frame |

Every export writes `<video>.manifest.json` to the output folder, listing the requested and
actual times of each clip, plus any failures. One bad segment never aborts a batch.

## Development

```sh
uv sync --all-extras
tests/make_fixtures.sh       # synthetic test media (the speech fixture needs macOS `say`)
uv run pytest                # ~100 tests, ~15 s
uv run pytest -m slow        # 1-hour / 3-hour file tests, ~1 min
```

`clipper.core` is plain Python with no MCP imports; `clipper.tools` is a thin MCP adapter over it.
