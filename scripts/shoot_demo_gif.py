"""Record the README's motion clip: one session streaming into the terminal.

The stills in `docs/screenshots/` show what the console looks like. They can't
show the thing it is actually for — output arriving while you watch a fleet of
sessions change state. This records that: Playwright drives a real browser
against the disposable demo backend, captures webm, and ffmpeg turns it into a
GIF small enough to sit in a README.

Driven by `shoot_docs.sh --gif`; it needs the live session that script spawns,
so it is not useful standalone.

Output: docs/screenshots/demo.gif
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "screenshots" / "demo.gif"

# Same localStorage seeding the stills use — a fresh profile renders the
# session tree collapsed, which hides the fleet the clip is meant to show.
TREE_EXPANDED = [
    "/home/dev",
    "/home/dev/code",
    "/home/dev/code/webapp",
    "/home/dev/code/platform-api",
    "/home/dev/code/design-system",
]

# Capture bigger than we ship, then scale down: text rendered at 2x and
# resampled reads far better in a 256-colour GIF than text rendered at 1x.
CAPTURE = (1400, 880)
GIF_WIDTH = 900
FPS = 10
DSF = 2


def _to_gif(webm: Path, dest: Path, seconds: float, start: float = 0.0) -> None:
    """webm → GIF via a two-pass palette, which is the difference between a
    3 MB file and a 12 MB one at the same visual quality.

    `start` drops the head of the recording. Playwright starts the video when
    the context opens, so the first second or so is the SPA booting — a blank
    page, which in a looping GIF reads as a stall every time it wraps."""
    with tempfile.TemporaryDirectory() as td:
        palette = Path(td) / "palette.png"
        vf = f"fps={FPS},scale={GIF_WIDTH}:-1:flags=lanczos"
        seek = ["-ss", f"{start:.2f}"] if start > 0 else []
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", *seek, "-t", str(seconds),
             "-i", str(webm), "-vf", f"{vf},palettegen=stats_mode=diff",
             str(palette)],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", *seek, "-t", str(seconds),
             "-i", str(webm), "-i", str(palette),
             "-lavfi", f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3",
             str(dest)],
            check=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--live-sid", required=True,
                    help="the genuinely-live session to record")
    ap.add_argument("--seconds", type=float, default=11.0)
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg not found — skipping the GIF", file=sys.stderr)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        vid_dir = Path(td)
        with sync_playwright() as p:
            # xterm.js renders the terminal into a WebGL canvas. Headless
            # video capture reads the compositor, and a GPU-backed canvas
            # frequently lands in the recording as a STALE frame — the DOM
            # around it animates while the terminal sits frozen, which is
            # exactly the thing the clip exists to show. Forcing software
            # rasterisation puts the canvas on the same path as everything
            # else. (Screenshots are unaffected either way; they force a
            # repaint, which is why the stills always looked right.)
            #
            # --force-device-scale-factor must match the context's
            # device_scale_factor below. Chromium composites the canvas at the
            # scale it was LAUNCHED with while device_scale_factor only moves
            # the emulated devicePixelRatio, so a mismatch scales the terminal
            # — and only the terminal — by the ratio between them. See the
            # DSF_FLAG comment in shoot_docs.py.
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--use-gl=swiftshader",
                    "--enable-unsafe-swiftshader",
                    "--disable-gpu-compositing",
                    f"--force-device-scale-factor={DSF}",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": CAPTURE[0], "height": CAPTURE[1]},
                device_scale_factor=DSF,
                ignore_https_errors=True,
                record_video_dir=str(vid_dir),
                record_video_size={"width": CAPTURE[0], "height": CAPTURE[1]},
            )
            expanded = json.dumps(json.dumps(TREE_EXPANDED))
            ctx.add_init_script(
                f"try {{ localStorage.setItem('csm.tree.expanded', {expanded}) }} "
                f"catch (e) {{}}"
            )
            ctx.add_init_script(
                "try { localStorage.setItem('csm.pwa.ios-hint-dismissed', '1') } "
                "catch (e) {}"
            )
            ctx.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                "  const s = document.createElement('style');"
                "  s.textContent = '.pwa-status{display:none !important}';"
                "  document.head.appendChild(s);"
                "});"
            )
            page = ctx.new_page()
            t0 = time.monotonic()
            try:
                page.goto(f"{args.base}/sessions/{args.live_sid}",
                          wait_until="load", timeout=20000)
            except Exception as e:
                print(f"could not open the live session: {e}", file=sys.stderr)
                return 1
            # Everything before the terminal has a painted canvas is the SPA
            # booting. Measure how long that took so the encoder can cut it.
            try:
                page.wait_for_function(
                    "() => { const c = document.querySelector('.xterm canvas');"
                    "        return !!c && c.width > 0 }",
                    timeout=15000,
                )
            except Exception:
                pass                       # fall back to keeping the head
            head = time.monotonic() - t0
            # Let the websocket attach and the replay start writing.
            page.wait_for_timeout(int(args.seconds * 1000))
            video = page.video
            ctx.close()          # flushes the webm
            browser.close()
            src = Path(video.path()) if video else None

        if src is None or not src.exists():
            print("no video captured", file=sys.stderr)
            return 1
        _to_gif(src, OUT, args.seconds, start=head)

    print(f"✓ {OUT.relative_to(REPO)}  {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
