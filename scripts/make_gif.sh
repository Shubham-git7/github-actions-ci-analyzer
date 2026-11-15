#!/usr/bin/env bash
# Assemble docs/media/frames/*.jpg (from scripts/capture_demo.py) into docs/media/demo.gif. Needs ffmpeg.
set -euo pipefail
cd "$(dirname "$0")/.."
ffmpeg -y -loglevel error -framerate "${FPS:-5}" -i docs/media/frames/%05d.jpg \
  -vf "scale=${WIDTH:-960}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" \
  -loop 0 docs/media/demo.gif
ls -lh docs/media/demo.gif
