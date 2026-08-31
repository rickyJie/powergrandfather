"""Playwright shooter for the documentation screenshots.

Assumes a *demo* CSM backend is already listening (see `scripts/shoot_docs.sh`,
which seeds a throwaway DB and boots one on port 8899). Never point this at
your real instance — the images would leak whatever is in your live database.

Output: docs/screenshots/*.png
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshots"

# Deterministic ids, mirrored from scripts/seed_demo.py.
SID_CHECKOUT = "11111111-1111-4111-8111-111111111111"
SID_FLAKY = "22222222-2222-4222-8222-222222222222"

DEMO_PREVIEW_FILE = "/tmp/pgf-demo-preview/pricing.ts"

# Shoot at 2x so the images stay sharp on a HiDPI display. See DSF_FLAG below
# for why the browser has to be told the same number twice.
DSF = float(os.environ.get("PGF_SHOOT_DSF", "2"))

# Chromium composites a canvas at the DEVICE scale it was launched with, while
# `device_scale_factor` only changes the emulated `window.devicePixelRatio`
# used for layout. The DOM does not care — it is laid out in CSS pixels either
# way — but xterm renders the terminal into a canvas, and that canvas comes
# out scaled by (emulated dpr / real device scale). Launch at 1 and emulate 2
# and every glyph in the terminal is twice the size it should be, in a
# screenshot where the UI around it is correct: measured cell pitch was 21 CSS
# px at 1x, 43 at 2x, 65 at 3x — exactly linear in the mismatch. Forcing the
# browser's own device scale to match collapses the ratio to 1, which is what
# a real browser on a real HiDPI screen has. (Real users were never affected;
# this is purely an artifact of emulating a dpr the browser doesn't have.)
DSF_FLAG = f"--force-device-scale-factor={DSF:g}"

# PGF_SHOOT_PROBE=1 dumps the terminal's real geometry. It is the one element
# on the page whose size is computed at runtime rather than set in CSS, so when
# a shot looks wrong this is what tells a scaling problem (canvas backing store
# != css box * dpr) from a viewport-too-narrow one.
PROBE_JS = """() => {
  const mount = document.querySelector('.xterm-mount');
  const screen = document.querySelector('.xterm-screen');
  if (!mount) return 'no .xterm-mount on this page';
  return JSON.stringify({
    dpr: window.devicePixelRatio,
    mountW: Math.round(mount.getBoundingClientRect().width),
    screen: screen
      ? [Math.round(screen.getBoundingClientRect().width),
         Math.round(screen.getBoundingClientRect().height)]
      : null,
    // Each entry should read backing == css * dpr. Anything else means the
    // terminal is being drawn at the wrong scale.
    canvases: [...document.querySelectorAll('.xterm canvas')].map(c => ({
      css: [Math.round(c.getBoundingClientRect().width),
            Math.round(c.getBoundingClientRect().height)],
      backing: [c.width, c.height],
    })),
  });
}"""


def act_open_notifications(page: Page) -> None:
    # The bell lives in the top bar; NotificationPanel.vue toggles on click.
    for sel in ("button[aria-label='Notifications']", ".notif-bell", "button:has-text('🔔')"):
        try:
            page.click(sel, timeout=2500)
            page.wait_for_timeout(900)
            return
        except Exception:
            continue
    print("   ! could not open the notifications panel")


def act_open_workflow(page: Page) -> None:
    try:
        page.click("text=nightly_refactor", timeout=4000)
        page.wait_for_timeout(1200)
    except Exception as e:
        print(f"   ! open_workflow: {e}")


def act_open_custom_alert(page: Page) -> None:
    page.evaluate("window.scrollBy(0, 1100)")
    page.wait_for_timeout(600)
    try:
        page.click("button:has-text('Custom rule')", timeout=5000)
        page.wait_for_timeout(1100)
    except Exception as e:
        print(f"   ! open_custom_alert: {e}")


def act_scroll_alerts(page: Page) -> None:
    page.evaluate("window.scrollBy(0, 1100)")
    page.wait_for_timeout(700)


ACTIONS = {
    "open_notifications": act_open_notifications,
    "open_workflow": act_open_workflow,
    "open_custom_alert": act_open_custom_alert,
    "scroll_alerts": act_scroll_alerts,
}

# Folder paths the demo cwds (/home/dev/code/{webapp,platform-api,design-system})
# collapse into in the session tree. Seeded into localStorage so the tree opens.
TREE_EXPANDED = [
    "/home/dev",
    "/home/dev/code",
    "/home/dev/code/webapp",
    "/home/dev/code/platform-api",
    "/home/dev/code/design-system",
]

# (route, filename, viewport, wait_ms, full_page, action)
TARGETS = [
    (f"/sessions/{SID_CHECKOUT}", "sessions-hero.png",    (1600, 1000), 3000, False, None),
    ("/sessions",                 "sessions-list.png",    (1600, 1000), 2500, False, None),
    (f"/sessions/{SID_FLAKY}",    "sessions-waiting.png", (1600, 1000), 2500, False, None),
    ("/sessions",                 "notifications.png",    (1600, 1000), 2500, False, "open_notifications"),
    ("/automation",               "workflows.png",        (1600, 1000), 2500, False, None),
    ("/automation",               "workflow-detail.png",  (1600, 1000), 2500, False, "open_workflow"),
    ("/agents",                   "agent-deck.png",       (1600, 1000), 2500, False, None),
    ("/tokens",                   "tokens.png",           (1600, 1000), 3500, False, None),
    ("/tokens",                   "tokens-full.png",      (1600, 1200), 3500, True,  None),
    ("/tokens",                   "agent-alerts.png",     (1600, 1000), 3500, False, "scroll_alerts"),
    ("/tokens",                   "agent-alert-modal.png", (1600, 1000), 3500, False, "open_custom_alert"),
    ("/budgets",                  "budgets.png",          (1600, 1000), 2500, False, None),
    ("/settings?section=sync",    "sync.png",             (1600, 1000), 3000, False, None),
    ("/settings?section=backup",  "backup.png",           (1600, 1000), 2000, False, None),
    ("/settings",                 "settings.png",         (1600, 1000), 2000, False, None),
    (f"/api/files/preview?path={DEMO_PREVIEW_FILE}",
                                  "file-preview.png",     (1600, 1100), 2000, False, None),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8899",
                    help="demo backend base URL (NOT your real instance)")
    ap.add_argument("--only", default=None,
                    help="comma-separated filename substrings to shoot")
    ap.add_argument("--live-sid", default=None,
                    help="id of the genuinely-live session the harness spawned; "
                         "the hero shot uses it so the terminal pane shows an "
                         "attached PTY instead of 'this session has ended'")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    wanted = [s.strip() for s in args.only.split(",")] if args.only else None

    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[DSF_FLAG])
        for route, fname, (w, h), wait_ms, full_page, action in TARGETS:
            if wanted and not any(s in fname for s in wanted):
                continue
            if args.live_sid and fname == "sessions-hero.png":
                route = f"/sessions/{args.live_sid}"
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=DSF,
                ignore_https_errors=True,
            )
            # The session tree persists its open folders in localStorage and
            # starts fully COLLAPSED, so a fresh browser profile — which every
            # shot gets — renders the fleet as three closed rows. That hides
            # the one thing the Sessions page is for. Seed the folder paths the
            # demo cwds produce before any script runs.
            _expanded = json.dumps(json.dumps(TREE_EXPANDED))
            ctx.add_init_script(
                f"try {{ localStorage.setItem('csm.tree.expanded', {_expanded}) }} "
                f"catch (e) {{}}"
            )
            # The service worker's "app is ready" toast floats over the
            # bottom-right of every page on a fresh profile, and every shot
            # gets a fresh profile. It is a real feature, just not one anybody
            # wants photographed on top of the Tokens charts.
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
            url = args.base + route
            print(f"→ {url}\n  → {fname}")
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                # networkidle never settles on pages holding a websocket open.
                page.goto(url, wait_until="load", timeout=20000)
            page.wait_for_timeout(wait_ms)
            if action:
                ACTIONS[action](page)
            if os.environ.get("PGF_SHOOT_PROBE"):
                print("   probe:", page.evaluate(PROBE_JS))
            dest = OUT / fname
            try:
                page.screenshot(path=str(dest), full_page=full_page)
                print(f"  ✓ {dest.stat().st_size // 1024} KB")
            except Exception as e:
                print(f"  ✗ {e}")
                failures += 1
            ctx.close()
        browser.close()

    print(f"\n{len(TARGETS)} targets, {failures} failed → {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
