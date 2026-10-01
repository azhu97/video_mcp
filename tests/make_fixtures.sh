#!/usr/bin/env bash
# Generate synthetic test videos. -g 60 at 30fps → a keyframe every 2s.
set -euo pipefail
cd "$(dirname "$0")/fixtures"

ffmpeg -v error -y \
  -f lavfi -i testsrc=duration=60:size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=440:duration=60 \
  -g 60 -c:v libx264 -c:a aac sample.mp4
