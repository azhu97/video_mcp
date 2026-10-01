#!/usr/bin/env bash
# Generate synthetic test media. Idempotent: skips files that already exist.
set -euo pipefail
mkdir -p "$(dirname "$0")/fixtures"
cd "$(dirname "$0")/fixtures"
ff() { ffmpeg -nostdin -v error -y "$@"; }

# 60s, keyframe every 2s (-g 60 @ 30fps) so keyframe snapping is predictable.
[ -f sample.mp4 ] || ff \
  -f lavfi -i testsrc=duration=60:size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=440:duration=60 \
  -g 60 -keyint_min 60 -sc_threshold 0 -c:v libx264 -c:a aac sample.mp4

# Known events: hard scene cuts at 15/30/45s, loud bursts at 10/30/50s.
[ -f events.mp4 ] || ff \
  -f lavfi -i testsrc2=duration=15:size=640x360:rate=30 \
  -f lavfi -i smptehdbars=duration=15:size=640x360:rate=30 \
  -f lavfi -i "mandelbrot=size=640x360:rate=30,trim=duration=15" \
  -f lavfi -i "rgbtestsrc=size=640x360:rate=30,trim=duration=15" \
  -f lavfi -i "aevalsrc=0.03*sin(2*PI*330*t)+if(between(mod(t\,20)\,10\,11)\,0.9*sin(2*PI*880*t)\,0):s=48000:d=60" \
  -filter_complex "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,format=yuv420p[v]" \
  -map "[v]" -map 4:a -g 60 -c:v libx264 -c:a aac events.mp4

# Other containers / shapes for robustness tests.
[ -f sample.mkv ] || ff -i sample.mp4 -t 20 -c copy sample.mkv
[ -f sample.mov ] || ff -i sample.mp4 -t 20 -c copy sample.mov
[ -f no_audio.mp4 ] || ff -i sample.mp4 -t 20 -an -c copy no_audio.mp4
[ -f audio_only.m4a ] || ff -i sample.mp4 -t 20 -vn -c copy audio_only.m4a
[ -f "spaced ünïcode name.mp4" ] || ff -i sample.mp4 -t 20 -c copy "spaced ünïcode name.mp4"

# Multiple audio tracks + a subtitle track.
if [ ! -f multi.mkv ]; then
  printf '1\n00:00:01,000 --> 00:00:04,000\nhello\n\n2\n00:00:10,000 --> 00:00:12,000\nworld\n' > subs.srt
  ff -i sample.mp4 -f lavfi -i sine=frequency=880:duration=20 -i subs.srt \
     -t 20 -map 0:v -map 0:a -map 1:a -map 2 -c:v copy -c:a aac -c:s srt multi.mkv
  rm subs.srt
fi

# Variable frame rate: alternate 30fps and 10fps sections.
[ -f vfr.mp4 ] || ff \
  -f lavfi -i "testsrc=duration=20:size=320x240:rate=30,select='lt(mod(t\,4)\,2)+not(mod(n\,3))'" \
  -fps_mode vfr -c:v libx264 vfr.mp4

# Scoreboard clock: the 40x20 box at (580,320) "ticks" once per second during 5-15s and
# 25-40s, is frozen otherwise, and flickers every frame during 45-50s (overlay hidden).
[ -f clock.mp4 ] || ff \
  -f lavfi -i testsrc2=duration=60:size=640x360:rate=30 \
  -f lavfi -i "color=size=40x20:rate=30:duration=60,format=yuv420p,geq=lum='if(between(T\,45\,50)\,random(1)*255\,if(between(T\,5\,15)+between(T\,25\,40)\,60+120*mod(floor(T)\,2)\,60))':cb=128:cr=128" \
  -filter_complex "[0:v][1:v]overlay=580:320,format=yuv420p" -g 60 -c:v libx264 clock.mp4

# Corrupted file: valid header, truncated body.
[ -f corrupted.mp4 ] || head -c 20000 sample.mp4 > corrupted.mp4

# Speech for transcript tests (macOS `say`; skipped elsewhere).
if [ ! -f speech.mp4 ] && command -v say >/dev/null; then
  say -o speech.aiff "Welcome to the demo. First we talk about setup. [[slnc 1500]] \
    Now the important part. The request hit a timeout after thirty seconds. [[slnc 1500]] \
    We fixed the timeout by adding retries. Thanks for watching."
  ff -f lavfi -i color=c=navy:size=320x240:rate=10 -i speech.aiff -shortest \
     -c:v libx264 -c:a aac speech.mp4
  rm speech.aiff
fi
