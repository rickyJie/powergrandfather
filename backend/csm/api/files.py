"""File preview + OSS jump + recent-files endpoints.

**Security posture (deliberate)**: user chose "any-path access" —
`/api/files/preview` and `/api/files/raw` will serve any file readable
by the uvicorn process, with no cwd sandbox. That is fine on the
single-user localhost deployment CSM is designed for. **On an explicitly
enabled LAN deployment (`host=0.0.0.0`), any peer on the same network can
read your filesystem via this endpoint.** If that matters, either bind
to `127.0.0.1` or add path-prefix policy here.

Endpoints:
- `GET /api/files/preview?path=...` — HTML page with syntax-highlighted
  code, rendered Markdown (source/render toggle), or `<img>` for
  images. All rendering is server-side (pygments + markdown-py) so the
  preview page is pure HTML with no CDN dependency.
- `GET /api/files/raw?path=...` — raw file bytes with a best-guess
  Content-Type. Used by the `<img>` inside preview and for downloads.
- `GET /api/files/oss-redirect?uri=s3://bucket/key` — 302 to
  `{settings.oss_base_url}/bucket/key`. Frontend just `window.open`s
  this URL and the browser follows the redirect.
- `GET /api/files/recent/{sid}?limit=50` — list of files claude has
  touched in this session (from PreToolUse hooks), newest first.
"""
from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import markdown as md_lib
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._assets import asset, script_tag
from csm.api._deps import get_db_sessionmaker
from csm.config import settings
from csm.models import SessionFileTouch
from csm.models.session import Session as SessionModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])

# Whitelist for OSS bucket and key segments used by /oss-redirect.
# Rationale: the endpoint 302s to `{oss_base_url}/{bucket}/{key}`, and if
# key contains `?`, `#`, or `..` an attacker can craft a phishing link
# that looks like it goes to CSM but actually redirects to an arbitrary
# path on the OSS host. Restrict to alphanumerics plus `.`, `_`, `-`, `/`.
_OSS_KEY_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


# ---------- helpers ----------

# File extensions that are safe to inline as `<img>` sources. Anything
# else that identifies as `image/*` via mimetypes still gets served by
# `/raw` — but we don't try to auto-render it inline.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
_MARKDOWN_EXTS = {".md", ".markdown", ".mdown"}
_HTML_EXTS = {".html", ".htm"}
_YAML_EXTS = {".yaml", ".yml"}


def _check_allowed_roots(p: Path) -> None:
    """C6 — optional root allowlist. Empty settings.file_preview_allowed_roots
    = allow anywhere (default). Non-empty = `p` must be under one of the
    listed roots or 403.

    Extracted so the /inline route (which composes its own absolute path
    from a b64 dir + relative filename) shares the same policy as _resolve.
    """
    if not settings.file_preview_allowed_roots:
        return
    for root in settings.file_preview_allowed_roots:
        try:
            root_resolved = Path(root).expanduser().resolve()
        except (ValueError, OSError):
            continue
        if p == root_resolved or p.is_relative_to(root_resolved):
            return
    raise HTTPException(
        status_code=403,
        detail=f"path not under any allowed root: {p}",
    )


def _resolve(path_str: str) -> Path:
    """Decode + resolve. Explicitly does NOT check cwd containment —
    any-path access is the deliberate design choice here.
    Raises HTTPException(400) for empty input.

    C6: If `settings.file_preview_allowed_roots` is non-empty, the
    resolved path must be under one of those roots or 403 is raised.
    Empty list (the default) preserves the original any-path behavior.
    """
    if not path_str:
        raise HTTPException(status_code=400, detail="missing 'path' query parameter")
    # Support `~/foo` — expanded via os.path.expanduser (uvicorn's HOME).
    expanded = os.path.expanduser(path_str)
    try:
        p = Path(expanded).resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    _check_allowed_roots(p)
    return p


def _encode_dir_b64(directory: Path) -> str:
    """URL-safe base64 of an absolute directory path, unpadded.

    Used by /inline to mount a directory into the URL space so that HTML
    files with relative <video>/<img>/<link>/<script> sub-resources can
    resolve them against sibling files under the same directory. The
    directory is opaque to the URL — no path segment is visible to the
    user or to embedded scripts inspecting document.URL.
    """
    raw = base64.urlsafe_b64encode(str(directory).encode("utf-8"))
    return raw.rstrip(b"=").decode("ascii")


def _decode_dir_b64(s: str) -> Path:
    """Inverse of `_encode_dir_b64`. Raises HTTPException(400) on garbage.
    Does NOT validate that the decoded path exists or is a directory —
    the caller does that after resolving so the error surface stays uniform.
    """
    pad = "=" * (-len(s) % 4)
    try:
        raw = base64.urlsafe_b64decode(s + pad)
        return Path(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid dir encoding: {e}")


def _inline_url_for(p: Path) -> str:
    """Return `/api/files/inline/{b64_dir}/{filename}` for a resolved file.
    The filename is passed as a single path segment (already safe since
    it's just the file's basename); sub-resources referenced inside the
    HTML get their own inline requests via directory-relative resolution.
    """
    from urllib.parse import quote as urlquote
    b64 = _encode_dir_b64(p.parent)
    return f"/api/files/inline/{b64}/{urlquote(p.name, safe='')}"


def _looks_binary(sample: bytes) -> bool:
    """Heuristic: if any of the first 4KB is NUL, treat as binary."""
    return b"\x00" in sample[:4096]


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _relative_time(mtime_epoch: float) -> str:
    """Human "2h ago" / "3d ago" for the header meta line. Tooltip in
    header still shows the absolute timestamp (via `title=`)."""
    delta = time.time() - mtime_epoch
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta // 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h}h ago"
    if delta < 30 * 86400:
        d = int(delta // 86400)
        return f"{d}d ago"
    if delta < 365 * 86400:
        mo = int(delta // (30 * 86400))
        return f"{mo}mo ago"
    y = int(delta // (365 * 86400))
    return f"{y}y ago"


# SVG icon glyphs (inline, no external assets). Feather / Lucide-style —
# 24×24 viewBox, stroke-only so `currentColor` tints them via CSS.
_ICON_CODE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>'
)
_ICON_MARKDOWN = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 5h18v14H3z"/><path d="M7 15V9l3 3 3-3v6"/><path d="M17 9v6"/>'
    '<polyline points="14 12 17 15 20 12"/></svg>'
)
_ICON_IMAGE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
    '<path d="M21 15l-5-5L5 21"/></svg>'
)
_ICON_DOC = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/></svg>'
)


def _file_type_icon(ext: str) -> str:
    """Pick an inline SVG glyph for the header slot."""
    if ext in _IMAGE_EXTS:
        return _ICON_IMAGE
    if ext in _MARKDOWN_EXTS:
        return _ICON_MARKDOWN
    if ext:  # any other ext with a value → code family
        return _ICON_CODE
    return _ICON_DOC


# --- yaml rendering ---------------------------------------------------------
# Structural tree render for `.yaml` / `.yml` — `local:5f872dbb` wanted yaml
# preview to be RENDERED (collapsible hierarchy with type badges), not raw
# syntax-highlighted source. Uses <details>/<summary> so folding is browser-
# native — no JS needed for expand/collapse. Scalars show inline; dicts and
# lists become collapsible sections. Types get a tiny badge so the user can
# skim structure without reading values.


def _render_yaml_scalar(v: Any) -> str:
    """One-line render for a scalar leaf value (str/int/float/bool/null)."""
    if v is None:
        return '<span class="yv yv-null">null</span>'
    if isinstance(v, bool):
        return f'<span class="yv yv-bool">{str(v).lower()}</span>'
    if isinstance(v, (int, float)):
        return f'<span class="yv yv-num">{v}</span>'
    if isinstance(v, str):
        # multiline strings render as a folded block so they don't blow
        # up the tree; single-line stays inline
        if "\n" in v:
            preview = v.split("\n", 1)[0][:60] + " …"
            return (
                '<details class="yv-block">'
                f'<summary><span class="yv yv-str">{_html_escape(preview)}</span>'
                f' <span class="yv-mult">({v.count(chr(10)) + 1} lines)</span></summary>'
                f'<pre class="yv-pre">{_html_escape(v)}</pre>'
                '</details>'
            )
        return f'<span class="yv yv-str">{_html_escape(v)}</span>'
    return f'<span class="yv">{_html_escape(str(v))}</span>'


def _render_yaml_node(v: Any, depth: int = 0) -> str:
    """Recursively render a parsed yaml node. Dicts and lists become
    `<details>` blocks with a type/size badge; scalars go through
    `_render_yaml_scalar`."""
    if isinstance(v, dict):
        if not v:
            return '<span class="yv yv-empty">{ } (empty map)</span>'
        parts = ['<ul class="yaml-map">']
        for k, val in v.items():
            key_esc = _html_escape(str(k))
            if isinstance(val, (dict, list)) and val:
                open_attr = " open" if depth < 1 else ""
                size = len(val)
                kind = "map" if isinstance(val, dict) else "list"
                parts.append(
                    f'<li class="yaml-node">'
                    f'<details class="yaml-details"{open_attr}>'
                    f'<summary><span class="yk">{key_esc}</span>'
                    f' <span class="yb yb-{kind}">{kind}·{size}</span></summary>'
                    f'{_render_yaml_node(val, depth + 1)}'
                    f'</details>'
                    f'</li>'
                )
            else:
                parts.append(
                    f'<li class="yaml-leaf">'
                    f'<span class="yk">{key_esc}</span>'
                    f'<span class="ysep">:</span>'
                    f'{_render_yaml_scalar(val)}'
                    f'</li>'
                )
        parts.append('</ul>')
        return "".join(parts)
    if isinstance(v, list):
        if not v:
            return '<span class="yv yv-empty">[ ] (empty list)</span>'
        parts = ['<ol class="yaml-list">']
        for i, item in enumerate(v):
            if isinstance(item, (dict, list)) and item:
                open_attr = " open" if depth < 1 else ""
                size = len(item)
                kind = "map" if isinstance(item, dict) else "list"
                parts.append(
                    f'<li class="yaml-node" value="{i}">'
                    f'<details class="yaml-details"{open_attr}>'
                    f'<summary><span class="yi">[{i}]</span>'
                    f' <span class="yb yb-{kind}">{kind}·{size}</span></summary>'
                    f'{_render_yaml_node(item, depth + 1)}'
                    f'</details>'
                    f'</li>'
                )
            else:
                parts.append(
                    f'<li class="yaml-leaf" value="{i}">'
                    f'<span class="yi">[{i}]</span>'
                    f'<span class="ysep">:</span>'
                    f'{_render_yaml_scalar(item)}'
                    f'</li>'
                )
        parts.append('</ol>')
        return "".join(parts)
    # top-level scalar (rare but valid yaml: `42`, `"hi"`, etc.)
    return f'<div class="yaml-toplevel-scalar">{_render_yaml_scalar(v)}</div>'


def _render_yaml_documents(text: str) -> tuple[str, str | None]:
    """Parse `text` as (possibly multi-doc) yaml and return the rendered HTML
    tree. Returns (html, error) — if parse fails, html is a syntax-error
    banner and error contains the exception message.
    """
    import yaml as _yaml  # local import so accidental removal from deps
                          # doesn't kill unrelated file preview code paths

    try:
        docs = list(_yaml.safe_load_all(text))
    except _yaml.YAMLError as e:
        # Line + column so user can spot the exact bad line even in a big file
        loc = ""
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            loc = f" (line {mark.line + 1}, col {mark.column + 1})"
        return (
            f'<div class="yaml-parse-err">'
            f'<div class="yaml-parse-err-title">YAML parse error{_html_escape(loc)}</div>'
            f'<pre class="yaml-parse-err-msg">{_html_escape(str(e))}</pre>'
            f'</div>',
            str(e),
        )

    if not docs:
        return '<div class="yaml-empty">(empty yaml document)</div>', None

    if len(docs) == 1:
        return f'<div class="yaml-tree">{_render_yaml_node(docs[0])}</div>', None

    # Multi-doc yaml: one section per document, separated visually
    parts = []
    for i, doc in enumerate(docs):
        parts.append(
            f'<section class="yaml-doc">'
            f'<div class="yaml-doc-header">--- document {i + 1} of {len(docs)}</div>'
            f'<div class="yaml-tree">{_render_yaml_node(doc)}</div>'
            f'</section>'
        )
    return "".join(parts), None


def _lexer_short_name(lexer: Any) -> str:
    """Short badge label for the header language pill — e.g. `PY`, `TS`,
    `CPP`. Falls back to the first alias when the primary name is long."""
    if lexer is None:
        return "TXT"
    aliases = getattr(lexer, "aliases", ())
    name: str = ""
    if aliases:
        name = aliases[0]
    else:
        name = getattr(lexer, "name", "") or "TXT"
    name = name.upper()
    # Some canonical shortcuts.
    _MAP = {
        "PYTHON": "PY",
        "PYTHON3": "PY",
        "JAVASCRIPT": "JS",
        "TYPESCRIPT": "TS",
        "MARKDOWN": "MD",
        "MAKEFILE": "MK",
        "BASH": "SH",
        "SHELL": "SH",
        "C++": "CPP",
        "OBJECTIVE-C": "OBJC",
    }
    if name in _MAP:
        return _MAP[name]
    return name[:4]


# ---------- preview HTML shell ----------

# Structure lives in `_assets/preview_head.html` and its CSS in
# `_assets/preview.css` — a `.py` string literal gave 596 lines of markup no
# highlighting, no linting, and no editor navigation. `{style}` is filled from
# the CSS asset; because `str.format` does not re-scan substituted values, the
# CSS keeps single braces instead of the 129 `{{` escapes it needed inline.
_HTML_HEAD = asset("preview_head.html")

# Concatenated, not `.format`ed — the JS is full of braces.
_HTML_FOOT = (
    "\n</div>\n"
    '<div id="toast" class="toast"><span></span></div>\n'
    + script_tag("preview.js")
    + "\n</body></html>\n"
)


def _dual_pygments_css(cssclass: str) -> str:
    """Light (tango) + dark (one-dark) pygments styles, scoped by
    `prefers-color-scheme` so the token colors track the shell theme.
    Same cssclass so the same `<span>` markup is styled either way.
    Choice rationale: tango + one-dark share a blue-lean palette so the
    theme switch reads as "same app, different lighting" rather than
    "completely different tool"."""
    light = HtmlFormatter(style="tango", cssclass=cssclass).get_style_defs(f".{cssclass}")
    dark = HtmlFormatter(style="one-dark", cssclass=cssclass).get_style_defs(f".{cssclass}")
    return (
        "@media (prefers-color-scheme: light) {\n" + light + "\n}\n"
        "@media (prefers-color-scheme: dark) {\n" + dark + "\n}\n"
    )


def _build_breadcrumb(path_str: str, max_visible_middle: int = 5) -> str:
    """Split a path into clickable segments. Last segment is bold.
    Leading `/` renders as a subtle root marker; `~` is preserved.

    When middle segments exceed `max_visible_middle`, collapse the
    interior into a single `…` marker with a `title=full-path` tooltip
    so long paths don't push the header actions off screen.
    """
    if not path_str:
        return ""
    prefix = ""
    remainder = path_str
    if remainder.startswith("~/"):
        prefix = "~"
        remainder = remainder[2:]
    elif remainder.startswith("/"):
        prefix = ""
        remainder = remainder[1:]
    segs = [s for s in remainder.split("/") if s]
    if not segs:
        return f'<span class="seg seg-last">{_html_escape(path_str)}</span>'

    parts: list[str] = []
    if prefix:
        parts.append(f'<span class="seg">{_html_escape(prefix)}</span>')
        parts.append('<span class="sep">/</span>')
    else:
        parts.append('<span class="sep">/</span>')

    # Fold when middle has too many segments. Keep first 2, ellipsis,
    # last 2 (or fewer if segs is short). `max_visible_middle` counts
    # non-terminal segments; ext count includes the filename.
    if len(segs) > max_visible_middle + 1:
        head, tail = segs[:2], segs[-2:]
        elided = "/".join(segs[2:-2])
        for seg in head:
            parts.append(f'<span class="seg">{_html_escape(seg)}</span>')
            parts.append('<span class="sep">/</span>')
        parts.append(
            f'<span class="seg-ellipsis" title="{_html_escape(elided)}">…</span>'
        )
        parts.append('<span class="sep">/</span>')
        for i, seg in enumerate(tail):
            cls = "seg seg-last" if i == len(tail) - 1 else "seg"
            parts.append(f'<span class="{cls}">{_html_escape(seg)}</span>')
            if i < len(tail) - 1:
                parts.append('<span class="sep">/</span>')
    else:
        for i, seg in enumerate(segs):
            cls = "seg seg-last" if i == len(segs) - 1 else "seg"
            parts.append(f'<span class="{cls}">{_html_escape(seg)}</span>')
            if i < len(segs) - 1:
                parts.append('<span class="sep">/</span>')
    return "".join(parts)


def _shell(
    path_str: str,
    title: str,
    body_html: str,
    pygments_css: str = "",
    lang_label: str = "",
    session_context: dict[str, Any] | None = None,
) -> str:
    """Wrap `body_html` in the full CSM preview shell.

    `lang_label` powers the header badge (e.g. `PY`, `TS`, `MD`); when
    empty, the badge is omitted (used by error / image branches).

    `session_context`, when provided, renders a secondary "back to session"
    context bar below the sticky header. Shape:
    `{"sid": str, "title": str | None, "agent": str | None, "mode":
    "preview" | "diff"}`. The mode drives which counterpart link
    (preview ↔ diff) shows on the right side of the bar.
    """
    try:
        st = os.stat(os.path.expanduser(path_str))
        size = _human_size(st.st_size)
        mtime_dt = datetime.fromtimestamp(st.st_mtime)
        mtime_abs = mtime_dt.strftime("%Y-%m-%d %H:%M:%S")
        mtime_rel = _relative_time(st.st_mtime)
    except OSError:
        size = mtime_abs = mtime_rel = "—"
    from urllib.parse import quote as urlquote

    # Ext determines file icon; None-safe.
    ext = Path(path_str).suffix.lower() if path_str else ""
    badge_html = (
        f'<span class="badge">{_html_escape(lang_label)}</span>' if lang_label else ""
    )
    head = _HTML_HEAD.format(
        style=asset("preview.css"),
        title=_html_escape(title),
        breadcrumb_html=_build_breadcrumb(path_str),
        path_escaped_attr=_html_escape(path_str),
        path_encoded=urlquote(path_str, safe=""),
        size=size,
        mtime_abs=_html_escape(mtime_abs),
        mtime_rel=_html_escape(mtime_rel),
        pygments_css=pygments_css,
        file_icon_svg=_file_type_icon(ext),
        badge_html=badge_html,
    )
    ctx_bar_html = (
        render_session_context_bar(path_str, session_context) if session_context else ""
    )
    return head + ctx_bar_html + body_html + _HTML_FOOT


# Reusable SVG icons for state cards.
_STATE_ICON_DANGER = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>'
    '<line x1="9" y1="9" x2="15" y2="15"/></svg>'
)
_STATE_ICON_INFO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/>'
    '<line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
)
_STATE_ICON_WARNING = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>'
    '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
)


def _state_card(
    tone: str,
    title_text: str,
    body_html: str,
    actions_html: str = "",
) -> str:
    """Render a state card in `.state-card.{tone}` — tone ∈ {danger, info, warning}."""
    icon = {
        "danger": _STATE_ICON_DANGER,
        "info": _STATE_ICON_INFO,
        "warning": _STATE_ICON_WARNING,
    }.get(tone, _STATE_ICON_INFO)
    return (
        f'<div class="state-card {tone}">'
        f'<div class="icon">{icon}</div>'
        f'<h2>{_html_escape(title_text)}</h2>'
        f'<p>{body_html}</p>'
        f'{actions_html}'
        f'</div>'
    )


# ---------- endpoints ----------

async def _lookup_session_cwd(sm: async_sessionmaker, session_id: str) -> Path | None:
    """Look up a session's cwd for relative-path preview resolution."""
    async with sm() as db:
        row = (await db.execute(
            select(SessionModel.cwd).where(SessionModel.id == session_id)
        )).scalar_one_or_none()
    return Path(row) if row else None


async def lookup_session_ctx(sm: async_sessionmaker, session_id: str) -> dict[str, Any] | None:
    """Look up minimal session metadata for the preview / diff context bar.

    Returns `{sid, title, agent, cwd}` or None if the row doesn't exist.
    Public helper (no leading underscore) because `sessions.py` calls it
    from the diff-view handler to build the same context strip.
    """
    async with sm() as db:
        row = (await db.execute(
            select(SessionModel.id, SessionModel.title, SessionModel.agent, SessionModel.cwd)
            .where(SessionModel.id == session_id)
        )).one_or_none()
    if row is None:
        return None
    sid, title, agent, cwd = row
    return {"sid": sid, "title": title, "agent": agent, "cwd": cwd}


def render_session_context_bar(path_str: str, ctx: dict[str, Any]) -> str:
    """Render the "← Back to session · title · Preview↔Diff" strip that
    sits between the shell header and the body.

    `ctx.mode` selects which counterpart link to render:
      * `"preview"` → "View diff" link → /api/sessions/{sid}/changes/diff-view?path=...
      * `"diff"`    → "Open file preview" link → /api/files/preview?path=...&session_id={sid}
    The counterpart is omitted when `path_str` is a synthetic identifier
    (e.g. `session-<sid8>-changes` used by the all-files diff view) so
    we don't emit a broken preview link.
    """
    from urllib.parse import quote as urlquote
    sid = str(ctx.get("sid") or "")
    title = ctx.get("title") or (sid[:8] if sid else "?")
    agent = ctx.get("agent") or ""
    mode = ctx.get("mode") or "preview"

    counterpart_html = ""
    path_is_real_file = bool(path_str) and not path_str.startswith("session-")
    if path_is_real_file and sid:
        path_enc = urlquote(path_str, safe="")
        if mode == "preview":
            counterpart_html = (
                f'<a class="csm-ctx-btn" '
                f'href="/api/sessions/{urlquote(sid)}/changes/diff-view?path={path_enc}" '
                f'target="_blank" rel="noopener" title="View this file\'s diff in the session">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                f'stroke-linecap="round" stroke-linejoin="round">'
                f'<line x1="4" y1="8" x2="20" y2="8"/><line x1="8" y1="16" x2="20" y2="16"/>'
                f'<line x1="4" y1="16" x2="4" y2="16"/></svg>'
                f'<span>View diff</span></a>'
            )
        else:  # diff
            counterpart_html = (
                f'<a class="csm-ctx-btn" '
                f'href="/api/files/preview?path={path_enc}&session_id={urlquote(sid)}" '
                f'target="_blank" rel="noopener" title="Open this file in the preview viewer">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                f'stroke-linecap="round" stroke-linejoin="round">'
                f'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>'
                f'<circle cx="12" cy="12" r="3"/></svg>'
                f'<span>Open file preview</span></a>'
            )

    back_href = f"/sessions/{urlquote(sid)}" if sid else "/sessions"
    agent_chip = (
        f'<span class="csm-ctx-agent">{_html_escape(agent)}</span>' if agent else ""
    )
    return (
        f'<div class="csm-ctx-bar">'
        f'  <a class="csm-ctx-back" href="{back_href}" title="Back to session in CSM (opens in same tab)">'
        f'    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'    <polyline points="15 18 9 12 15 6"/></svg>'
        f'    <span>Back to session</span>'
        f'  </a>'
        f'  <span class="csm-ctx-sep">·</span>'
        f'  <span class="csm-ctx-title" title="{_html_escape(str(title))}">{_html_escape(str(title))}</span>'
        f'  {agent_chip}'
        f'  <span class="csm-ctx-spacer"></span>'
        f'  {counterpart_html}'
        f'</div>'
    )


async def _resolve_for_preview(
    path_str: str,
    session_id: str | None,
    sm: async_sessionmaker | None,
) -> Path:
    """Preview-only resolver: absolute / ~-prefixed paths fall through to
    the plain _resolve helper (which still handles cwd-anchored resolves
    against the uvicorn cwd for backward-compat callers with no sid).

    A path that looks relative (no leading `/` or `~`) is resolved against
    the given session's cwd if a session_id is provided. The resolved
    absolute path must remain under the session's cwd — anything that
    escapes via `..` returns 403. This is the sole surface where users
    can drive path resolution off untrusted input, so the containment
    check is non-negotiable.
    """
    if not path_str:
        raise HTTPException(status_code=400, detail="missing 'path' query parameter")
    is_absish = path_str.startswith("/") or path_str.startswith("~")
    if is_absish or not session_id or sm is None:
        return _resolve(path_str)
    cwd = await _lookup_session_cwd(sm, session_id)
    if cwd is None:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    try:
        cwd_resolved = cwd.expanduser().resolve()
        candidate = (cwd_resolved / path_str).resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    if candidate != cwd_resolved and not candidate.is_relative_to(cwd_resolved):
        raise HTTPException(
            status_code=403,
            detail=f"relative path escapes session cwd: {path_str}",
        )
    if settings.file_preview_allowed_roots:
        allowed = False
        for root in settings.file_preview_allowed_roots:
            try:
                root_resolved = Path(root).expanduser().resolve()
                if candidate == root_resolved or candidate.is_relative_to(root_resolved):
                    allowed = True
                    break
            except (ValueError, OSError):
                continue
        if not allowed:
            raise HTTPException(status_code=403, detail=f"path not under any allowed root: {candidate}")
    return candidate


@router.get("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    path: str = Query(..., description="Absolute, ~-prefixed, or session-relative file path"),
    session_id: str | None = Query(
        None,
        description="If path is relative, resolve against this session's cwd. Ignored for absolute paths.",
    ),
):
    # OSS pass-through: if the caller handed us an s3:// URI (or the
    # `//bucket/key` residue you get when a browser strips the `s3:`
    # scheme from address-bar input), don't try to render HTML — just
    # 302 through the oss-redirect endpoint so the shared allowlist
    # regex + `..` guard still apply.
    if path.startswith("s3://") or path.startswith("//"):
        s3_uri = path if path.startswith("s3://") else "s3:" + path
        from urllib.parse import quote as urlquote
        return RedirectResponse(
            url=f"/api/files/oss-redirect?uri={urlquote(s3_uri, safe='')}",
            status_code=302,
        )
    # Lazily fetch sm — only relative+session_id path uses it, so allowlist-only
    # test fixtures that never wire app.state.sessionmaker still work for
    # absolute paths.
    sm = getattr(request.app.state, "sessionmaker", None)
    p = await _resolve_for_preview(path, session_id, sm)
    # Look up session context for the back-to-session bar. Only when
    # session_id was passed AND we can find that row; missing session_id
    # or missing row → no context bar, no counterpart link (preview
    # keeps working stand-alone).
    ctx: dict[str, Any] | None = None
    if session_id and sm is not None:
        row = await lookup_session_ctx(sm, session_id)
        if row is not None:
            ctx = {**row, "mode": "preview"}
    if not p.exists():
        body = _state_card(
            "danger",
            "File not found",
            f"The path <code>{_html_escape(str(p))}</code> doesn't exist "
            "or has been moved.",
            f'<div class="actions">'
            f'<button class="btn btn-ghost" type="button" data-copy-path="{_html_escape(path)}">Copy attempted path</button>'
            f'<a class="btn" href="javascript:location.reload()">Retry</a>'
            f'</div>',
        )
        return HTMLResponse(_shell(path, p.name, body, session_context=ctx), status_code=404)
    if p.is_dir():
        body = _state_card(
            "info",
            "That's a directory",
            f"<code>{_html_escape(str(p))}</code> is a directory. "
            "Directory browsing is not implemented yet — use <code>ls</code> "
            "in a terminal for now.",
            f'<div class="actions">'
            f'<button class="btn btn-ghost" type="button" data-copy-path="{_html_escape(str(p))}">Copy path</button>'
            f'</div>',
        )
        return HTMLResponse(_shell(path, p.name, body, session_context=ctx), status_code=400)
    try:
        size = p.stat().st_size
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"stat failed: {e}")

    ext = p.suffix.lower()
    from urllib.parse import quote as urlquote
    # Encode the RESOLVED absolute path so generated <img> / download links
    # work without needing the session_id round-trip. Relative paths that
    # went through _resolve_for_preview end up here as absolute.
    path_encoded = urlquote(str(p), safe="")

    # Images: inline via /raw (streamed) — no size check because the
    # browser handles the payload streaming; skipping the read avoids
    # loading a huge photo into memory just to render an <img>.
    if ext in _IMAGE_EXTS:
        body = (
            f'<div class="image-frame">'
            f'<img class="preview" src="/api/files/raw?path={path_encoded}" alt="{_html_escape(p.name)}">'
            f'</div>'
        )
        return HTMLResponse(_shell(path, p.name, body, lang_label=ext.lstrip(".").upper(), session_context=ctx))

    # Size gate — HTML gets its own (higher) cap because the render tab
    # streams through an iframe (`/inline/...`) instead of reading the
    # whole file into memory; only the Source tab pays the read + pygments
    # cost. Real-world data-viz reports (Plotly / Chart.js dumps) land in
    # the 3-8 MB range, so bump their limit rather than force a download.
    max_bytes = (
        settings.file_preview_html_max_bytes
        if ext in _HTML_EXTS
        else settings.file_preview_max_bytes
    )
    if size > max_bytes:
        body = _state_card(
            "info",
            "File too large to preview",
            f"This file is <strong>{_human_size(size)}</strong>, over the "
            f"{_human_size(max_bytes)} preview cap. "
            "Download to view locally.",
            f'<div class="actions">'
            f'<a class="btn btn-primary" href="/api/files/raw?path={path_encoded}" download>Download</a>'
            f'</div>',
        )
        return HTMLResponse(_shell(path, p.name, body, session_context=ctx))

    try:
        raw = p.read_bytes()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")

    if _looks_binary(raw):
        body = _state_card(
            "info",
            "Binary file",
            "This file contains non-text bytes (NUL) and can't be previewed inline.",
            f'<div class="actions">'
            f'<a class="btn btn-primary" href="/api/files/raw?path={path_encoded}" download>Download raw</a>'
            f'</div>',
        )
        return HTMLResponse(_shell(path, p.name, body, session_context=ctx))

    text = raw.decode("utf-8", errors="replace")

    # HTML: rendered by default in a full-height iframe. `sandbox=
    # "allow-scripts"` lets Plotly / Chart.js / vanilla-JS reports run
    # while withholding same-origin — the script cannot read this
    # document's cookies / localStorage or hit /api/* with the user's
    # session. `allow-same-origin` is deliberately absent.
    #
    # `allow-fullscreen` (via the modern `allowfullscreen` attr) lets the
    # inline `<button>Fullscreen</button>` request fullscreen on the
    # iframe so a data-viz report fills the screen without leaving the
    # preview shell.
    #
    # Iframe src is /api/files/inline/{b64_dir}/{filename} (NOT /raw)
    # because relative sub-resources — `<video src="./x.mp4">`,
    # `<img src="assets/foo.png">`, `<link href="./styles.css">`,
    # `<script src="./bundle.js">` — resolve against the URL path in the
    # browser. With `/raw?path=…` they'd collapse to `/api/files/x.mp4`
    # and 404; with the inline directory-mounted URL they map to sibling
    # files in the source file's parent directory.
    if ext in _HTML_EXTS:
        # Skip pygments syntax highlight for the Source tab on big HTMLs —
        # highlighting a 5-10 MB file takes several seconds server-side
        # AND balloons the response by ~3× (each token wrapped in a
        # `<span class>`). The Rendered tab (iframe) is the primary path
        # for these files anyway; users rarely need syntax colors on a
        # multi-megabyte data-viz dump. Threshold reuses the generic
        # text cap: anything the plain text/code branch would have
        # inlined gets highlighted; larger HTMLs fall back to raw <pre>.
        try:
            html_lexer = get_lexer_by_name("html")
        except ClassNotFound:
            html_lexer = None
        html_formatter = HtmlFormatter(
            style="tango", cssclass="pyghi", linenos="inline", lineseparator="\n",
        )
        if html_lexer is not None and size <= settings.file_preview_max_bytes:
            try:
                html_source = highlight(text, html_lexer, html_formatter)
            except Exception:
                log.exception("pygments highlight failed for %s", p)
                html_source = f"<pre>{_html_escape(text)}</pre>"
        else:
            html_source = f"<pre>{_html_escape(text)}</pre>"
        pygments_css = _dual_pygments_css("pyghi")
        inline_src = _inline_url_for(p)
        filename_esc = _html_escape(p.name)
        body = f"""
<style>
  /* Fullscreen HTML preview: make .body a flex column that fills the
     remaining viewport under the sticky header + context bar, and let
     the iframe wrapper grow to fill it. Overrides .body's default
     padding/max-width for this branch only — inline in body_html so we
     don't leak into other file types. */
  html, body {{ height: 100%; overflow: hidden; }}
  body {{ display: flex; flex-direction: column; }}
  .body {{
    flex: 1 1 auto;
    min-height: 0;
    padding: 0;
    max-width: none;
    margin: 0;
    display: flex;
    flex-direction: column;
  }}
  .csm-ctx-bar {{ flex: 0 0 auto; }}
  .html-tools {{
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    background: var(--bg-subtle);
    border-bottom: 1px solid var(--border);
    font-size: 12.5px;
  }}
  .html-tools .filename {{
    font-family: 'JetBrains Mono', 'SFMono-Regular', Menlo, Consolas, monospace;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }}
  .html-tools .spacer {{ flex: 1 1 auto; }}
  .html-tools .tool {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--bg-elevated);
    color: var(--text);
    text-decoration: none;
    cursor: pointer;
    font-size: 12px;
    line-height: 1;
    white-space: nowrap;
  }}
  .html-tools .tool:hover {{
    color: var(--accent);
    border-color: var(--accent);
    background: var(--accent-subtle);
  }}
  .html-tools .tool svg {{ width: 13px; height: 13px; }}
  .html-iframe-wrap {{ flex: 1 1 auto; min-height: 0; background: #fff; position: relative; }}
  .html-iframe {{ width: 100%; height: 100%; border: 0; background: #fff; display: block; }}
  /* Slide-over source panel, hidden by default. Fixed-position so it
     overlays the iframe without triggering a reflow. */
  .html-source-panel {{
    position: fixed;
    top: 0; right: 0; bottom: 0;
    width: min(80vw, 960px);
    background: var(--bg-elevated);
    border-left: 1px solid var(--border);
    box-shadow: var(--shadow-lg);
    overflow: auto;
    z-index: 100;
    transform: translateX(100%);
    transition: transform 0.18s ease-out;
    display: flex;
    flex-direction: column;
  }}
  .html-source-panel.open {{ transform: translateX(0); }}
  .html-source-head {{
    position: sticky; top: 0;
    display: flex; align-items: center; gap: 10px;
    padding: 8px 14px;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
    font-size: 12.5px;
    color: var(--text-muted);
  }}
  .html-source-head strong {{ color: var(--text); font-weight: 600; }}
  .html-source-head .close-x {{
    margin-left: auto;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--bg-subtle);
    padding: 3px 10px;
    border-radius: var(--radius-sm);
    color: var(--text);
  }}
  .html-source-head .close-x:hover {{ color: var(--accent); border-color: var(--accent); }}
  .html-source-body {{ padding: 12px 14px; overflow: auto; }}
</style>
<div class="html-tools">
  <span class="filename" title="{filename_esc}">{filename_esc}</span>
  <span class="spacer"></span>
  <button type="button" class="tool" id="html-tool-source" title="View HTML source">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
    Source
  </button>
  <a class="tool" href="/api/files/raw?path={path_encoded}" target="_blank" rel="noopener noreferrer" title="Open raw file in a new tab (no sandbox)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
    Open raw
  </a>
  <button type="button" class="tool" id="html-tool-fullscreen" title="Fullscreen the rendered preview">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 4 20 10 20"/><polyline points="20 10 20 4 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
    Full
  </button>
</div>
<div class="html-iframe-wrap">
  <iframe id="html-frame" class="html-iframe" src="{inline_src}" sandbox="allow-scripts" allowfullscreen title="HTML preview"></iframe>
</div>
<aside class="html-source-panel" id="html-source-panel" aria-hidden="true">
  <div class="html-source-head">
    <strong>{filename_esc}</strong>
    <span>source</span>
    <button type="button" class="close-x" id="html-source-close" title="Close (Esc)">Close ✕</button>
  </div>
  <div class="html-source-body">{html_source}</div>
</aside>
<script>
  (function() {{
    var srcBtn = document.getElementById('html-tool-source');
    var fsBtn = document.getElementById('html-tool-fullscreen');
    var panel = document.getElementById('html-source-panel');
    var closeBtn = document.getElementById('html-source-close');
    var frame = document.getElementById('html-frame');
    function openPanel()  {{ panel.classList.add('open'); panel.setAttribute('aria-hidden', 'false'); }}
    function closePanel() {{ panel.classList.remove('open'); panel.setAttribute('aria-hidden', 'true'); }}
    srcBtn.addEventListener('click', function() {{
      panel.classList.contains('open') ? closePanel() : openPanel();
    }});
    closeBtn.addEventListener('click', closePanel);
    document.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape' && panel.classList.contains('open')) closePanel();
    }});
    fsBtn.addEventListener('click', function() {{
      if (document.fullscreenElement) {{
        document.exitFullscreen();
      }} else if (frame.requestFullscreen) {{
        frame.requestFullscreen();
      }}
    }});
  }})();
</script>
"""
        return HTMLResponse(_shell(path, p.name, body, pygments_css, lang_label="HTML", session_context=ctx))

    # Markdown: source/rendered dual view.
    if ext in _MARKDOWN_EXTS:
        try:
            # arithmatex — LaTeX math ($…$ / $$…$$ / \(…\) / \[…\]) is
            # emitted as `<span class="arithmatex">$…$</span>`. KaTeX
            # auto-render (loaded via CDN below) scans the DOM and
            # renders those in-place. `generic=True` accepts both dollar
            # and backslash delimiters.
            rendered = md_lib.markdown(
                text,
                extensions=[
                    "fenced_code", "tables", "codehilite", "toc",
                    "pymdownx.arithmatex",
                ],
                extension_configs={
                    "codehilite": {"guess_lang": False, "css_class": "codehilite"},
                    "pymdownx.arithmatex": {"generic": True},
                },
            )
        except Exception:
            log.exception("markdown render failed for %s", p)
            rendered = f"<pre>{_html_escape(text)}</pre>"
        pygments_css = _dual_pygments_css("codehilite")
        body = f"""
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
        onload="renderMathInElement(document.body, {{
          delimiters: [
            {{ left: '$$', right: '$$', display: true }},
            {{ left: '$', right: '$', display: false }},
            {{ left: '\\\\(', right: '\\\\)', display: false }},
            {{ left: '\\\\[', right: '\\\\]', display: true }}
          ],
          throwOnError: false,
          ignoredTags: ['script','noscript','style','textarea','pre','code']
        }});"></script>
<div class="tabs">
  <button class="tab-btn active" data-view="rendered">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
    Rendered
  </button>
  <button class="tab-btn" data-view="source">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
    Source
  </button>
</div>
<div id="v-rendered" class="md-layout">
  <div class="md-body">{rendered}</div>
  <aside class="md-toc" id="md-toc"><p class="toc-title">On this page</p></aside>
</div>
<pre id="v-source" style="display:none">{_html_escape(text)}</pre>
<script>
  (function() {{
    var btns = document.querySelectorAll('.tab-btn');
    var vr = document.getElementById('v-rendered'), vs = document.getElementById('v-source');
    btns.forEach(function(b) {{
      b.addEventListener('click', function() {{
        btns.forEach(function(x) {{ x.classList.remove('active'); }});
        b.classList.add('active');
        var v = b.getAttribute('data-view');
        vr.style.display = v === 'rendered' ? '' : 'none';
        vs.style.display = v === 'source' ? '' : 'none';
      }});
    }});
  }})();
</script>
"""
        return HTMLResponse(_shell(path, p.name, body, pygments_css, lang_label="MD", session_context=ctx))

    # YAML: rendered as a collapsible tree by default with a Source toggle.
    # `local:5f872dbb` — user wanted yaml to be RENDERED (structural view)
    # not just syntax-highlighted source.
    if ext in _YAML_EXTS:
        rendered_html, parse_err = _render_yaml_documents(text)
        try:
            yaml_lexer = get_lexer_by_name("yaml")
        except ClassNotFound:
            yaml_lexer = None
        source_formatter = HtmlFormatter(
            style="tango", cssclass="pyghi", linenos="inline", lineseparator="\n",
        )
        if yaml_lexer is not None:
            try:
                source_html = highlight(text, yaml_lexer, source_formatter)
            except Exception:
                log.exception("pygments highlight failed for yaml %s", p)
                source_html = f"<pre>{_html_escape(text)}</pre>"
        else:
            source_html = f"<pre>{_html_escape(text)}</pre>"
        pygments_css = _dual_pygments_css("pyghi")
        # If yaml failed to parse, default the visible tab to Source so the
        # user immediately sees the raw file and the parse-error banner
        # sits on the (invisible) Rendered tab as a fallback breadcrumb.
        default_view = "source" if parse_err else "rendered"
        body = f"""
<style>
  .yaml-tree {{ font-family: 'Geist Mono', ui-monospace, Menlo, monospace; font-size: 13px; }}
  .yaml-tree ul, .yaml-tree ol {{ list-style: none; margin: 0; padding-left: 20px; border-left: 1px dashed var(--border, #e5e5e5); }}
  .yaml-tree li {{ padding: 2px 0; line-height: 1.5; }}
  .yaml-tree summary {{ cursor: pointer; user-select: none; }}
  .yaml-tree summary:hover {{ color: var(--fg-emphasis, #0366d6); }}
  .yaml-tree details[open] > summary {{ font-weight: 600; }}
  .yk {{ color: var(--fg-key, #6f42c1); font-weight: 500; }}
  .yi {{ color: var(--fg-index, #8a919a); font-variant-numeric: tabular-nums; }}
  .ysep {{ color: var(--fg-muted, #8a919a); margin: 0 6px 0 2px; }}
  .yv {{ display: inline; }}
  .yv-str {{ color: var(--fg-str, #032f62); }}
  .yv-num {{ color: var(--fg-num, #005cc5); font-variant-numeric: tabular-nums; }}
  .yv-bool {{ color: var(--fg-bool, #d73a49); }}
  .yv-null {{ color: var(--fg-null, #8a919a); font-style: italic; }}
  .yv-empty {{ color: var(--fg-muted, #8a919a); font-style: italic; }}
  .yv-mult {{ color: var(--fg-muted, #8a919a); font-size: 11px; margin-left: 4px; }}
  .yv-block > summary {{ font-weight: normal !important; }}
  .yv-pre {{
    margin: 4px 0 4px 0; padding: 8px 12px;
    background: var(--bg-inset, #f6f8fa);
    border-left: 3px solid var(--border-strong, #d0d7de);
    border-radius: 4px;
    font-size: 12px; white-space: pre-wrap; overflow-x: auto;
  }}
  .yb {{
    display: inline-block; margin-left: 4px; padding: 0 6px;
    font-size: 10px; font-weight: 600; letter-spacing: 0.03em;
    color: var(--fg-muted, #8a919a);
    background: var(--bg-inset, #f1f5f9);
    border-radius: 10px; vertical-align: middle;
  }}
  .yb-map {{ color: var(--fg-key, #6f42c1); }}
  .yb-list {{ color: var(--fg-num, #005cc5); }}
  .yaml-doc {{ margin-bottom: 24px; }}
  .yaml-doc-header {{
    font-family: 'Geist Mono', monospace; font-size: 11px;
    color: var(--fg-muted, #8a919a); margin-bottom: 8px;
    padding-bottom: 4px; border-bottom: 1px solid var(--border, #e5e5e5);
  }}
  .yaml-empty {{ color: var(--fg-muted, #8a919a); font-style: italic; padding: 12px; }}
  .yaml-parse-err {{
    padding: 12px 16px; margin-bottom: 16px;
    background: var(--bg-danger-subtle, #ffebe9);
    border: 1px solid var(--border-danger, #ff8182);
    border-radius: 6px; color: var(--fg-danger, #cf222e);
  }}
  .yaml-parse-err-title {{ font-weight: 600; margin-bottom: 6px; }}
  .yaml-parse-err-msg {{
    margin: 0; padding: 6px 8px;
    background: rgba(0,0,0,0.05); border-radius: 4px;
    font-family: 'Geist Mono', monospace; font-size: 12px;
    white-space: pre-wrap;
  }}
  .yaml-toplevel-scalar {{ padding: 12px; }}
</style>
<div class="tabs">
  <button class="tab-btn{' active' if default_view == 'rendered' else ''}" data-view="rendered">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
    Tree
  </button>
  <button class="tab-btn{' active' if default_view == 'source' else ''}" data-view="source">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
    Source
  </button>
</div>
<div id="v-rendered" class="yaml-rendered" style="display:{'' if default_view == 'rendered' else 'none'}">
  {rendered_html}
</div>
<div id="v-source" style="display:{'' if default_view == 'source' else 'none'}">
  {source_html}
</div>
<script>
  (function() {{
    var btns = document.querySelectorAll('.tab-btn');
    var vr = document.getElementById('v-rendered'), vs = document.getElementById('v-source');
    btns.forEach(function(b) {{
      b.addEventListener('click', function() {{
        btns.forEach(function(x) {{ x.classList.remove('active'); }});
        b.classList.add('active');
        var v = b.getAttribute('data-view');
        vr.style.display = v === 'rendered' ? '' : 'none';
        vs.style.display = v === 'source' ? '' : 'none';
      }});
    }});
  }})();
</script>
"""
        lang_label = "YAML" + (" ⚠" if parse_err else "")
        return HTMLResponse(_shell(path, p.name, body, pygments_css, lang_label=lang_label, session_context=ctx))

    # Code / plain text via pygments.
    try:
        lexer = get_lexer_for_filename(p.name, code=text)
    except ClassNotFound:
        try:
            lexer = get_lexer_by_name("text")
        except ClassNotFound:
            lexer = None
    formatter = HtmlFormatter(
        style="tango",  # base style; the @media override in _dual_pygments_css tints per theme
        cssclass="pyghi",
        linenos="inline",
        lineseparator="\n",
    )
    if lexer is not None:
        try:
            highlighted = highlight(text, lexer, formatter)
        except Exception:
            log.exception("pygments highlight failed for %s", p)
            highlighted = f"<pre>{_html_escape(text)}</pre>"
    else:
        highlighted = f"<pre>{_html_escape(text)}</pre>"
    pygments_css = _dual_pygments_css("pyghi")

    lang_label = _lexer_short_name(lexer)
    line_count = text.count("\n") + (0 if text.endswith("\n") else 1)
    # Wrap in a code-container with a toolbar (language, encoding, line count, Wrap, Copy).
    body = (
        f'<div class="code-container">'
        f'  <div class="code-toolbar">'
        f'    <div class="code-meta">'
        f'      <span>{_html_escape(getattr(lexer, "name", "Text"))}</span>'
        f'      <span class="dot">·</span><span>UTF-8</span>'
        f'      <span class="dot">·</span><span>{line_count} lines</span>'
        f'    </div>'
        f'    <div class="code-actions">'
        f'      <button class="icon-btn" type="button" data-toggle-wrap title="Toggle wrap (w)">'
        f'        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><polyline points="3 12 15 12 15 18 3 18"/><path d="M15 12h6"/></svg>Wrap</button>'
        f'      <button class="icon-btn" type="button" data-copy-code title="Copy code">'
        f'        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy</button>'
        f'    </div>'
        f'  </div>'
        f'  <div class="code-scroll">{highlighted}</div>'
        f'</div>'
    )
    return HTMLResponse(_shell(path, p.name, body, pygments_css, lang_label=lang_label, session_context=ctx))


@router.get("/raw")
def raw(path: str = Query(..., description="Absolute or ~-prefixed file path")) -> FileResponse:
    p = _resolve(path)
    if not p.exists() or p.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    media_type, _ = mimetypes.guess_type(p.name)
    # `content_disposition_type="inline"` so the sandboxed iframe in
    # /preview actually renders HTML instead of triggering a download,
    # and "Open raw ↗" opens in a new tab instead of downloading.
    # Download buttons in the preview shell use the HTML5 `download`
    # attribute, which overrides inline disposition browser-side.
    return FileResponse(
        str(p),
        media_type=media_type or "application/octet-stream",
        filename=p.name,
        content_disposition_type="inline",
    )


@router.get("/inline/{dir_b64}/{filename:path}")
def inline(dir_b64: str, filename: str) -> FileResponse:
    """Serve a file under a URL-mounted directory so relative sub-resources
    resolve normally in the browser.

    Motivation: HTML previews load through an iframe. When the iframe src
    is `/api/files/raw?path=…`, the browser resolves `<video src="./x.mp4">`
    against the URL PATH (`/api/files/raw`), not against the source file's
    parent directory — so videos, images, sibling css/js all 404. Serving
    the HTML from `/api/files/inline/{b64}/{filename}` instead means the
    browser resolves `./x.mp4` to `/api/files/inline/{b64}/x.mp4`, which
    then reads the sibling file from disk.

    Security: same posture as /raw — any file readable by uvicorn is
    reachable. The `file_preview_allowed_roots` allowlist (C6) is enforced
    against both the base dir and the composed target. `..` escapes are
    rejected by requiring the resolved target to be under the base dir.
    """
    base_dir = _decode_dir_b64(dir_b64)
    try:
        base_resolved = base_dir.expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid base dir: {e}")
    if not base_resolved.is_dir():
        raise HTTPException(status_code=404, detail="base directory not found")
    _check_allowed_roots(base_resolved)

    # Compose then resolve to canonicalize any `..` in filename, then
    # verify containment. This is the only place untrusted URL path
    # segments drive filesystem access, so the containment check is the
    # non-negotiable boundary.
    try:
        target = (base_resolved / filename).resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid filename: {e}")
    if target != base_resolved and not target.is_relative_to(base_resolved):
        raise HTTPException(
            status_code=403,
            detail="filename escapes base directory",
        )
    _check_allowed_roots(target)
    if not target.exists() or target.is_dir():
        raise HTTPException(status_code=404, detail="file not found")

    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
        content_disposition_type="inline",
    )


@router.get("/oss-redirect")
def oss_redirect(uri: str = Query(..., description="e.g. s3://bucket/key")) -> RedirectResponse:
    if not uri.startswith("s3://"):
        raise HTTPException(status_code=400, detail="only s3:// URIs supported currently")
    if not settings.oss_base_url:
        raise HTTPException(status_code=503, detail="oss_base_url is unset — cannot redirect")
    key = uri[len("s3://"):].lstrip("/")
    if not key:
        raise HTTPException(status_code=400, detail="empty s3 key")
    # Reject query / fragment / anchor injection first so the error is
    # crisper than "invalid characters" from the whitelist regex below.
    if any(c in key for c in "?#&"):
        raise HTTPException(
            status_code=400,
            detail="OSS key cannot contain query/fragment characters",
        )
    if not _OSS_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail=f"invalid OSS key (allowed: alphanumeric + . _ - /): {key!r}",
        )
    # Path-traversal guard: `..` as a full segment (splits catch both
    # leading, middle, and trailing `..`).
    if ".." in key.split("/"):
        raise HTTPException(
            status_code=400,
            detail="OSS key cannot contain '..' segments",
        )
    target = f"{settings.oss_base_url.rstrip('/')}/{key}"
    return RedirectResponse(url=target, status_code=302)


@router.get("/recent/{sid}")
async def recent(
    sid: str,
    limit: int = Query(50, ge=1, le=200),
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    async with sm() as db:
        res = await db.execute(
            select(SessionFileTouch)
            .where(SessionFileTouch.sid == sid)
            .order_by(SessionFileTouch.ts.desc(), SessionFileTouch.id.desc())
            .limit(limit)
        )
        rows = list(res.scalars().all())
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "path": r.path,
                "tool": r.tool,
                "ts": r.ts.isoformat() if r.ts else None,
            }
            for r in rows
        ],
    }


# Exposed for use by session purge handler / test cleanup — not a
# public endpoint.
async def prune_session_file_touches(db, sid: str, keep: int = 100) -> int:
    """Delete all but the newest `keep` touches for `sid`. Returns
    number of rows deleted. Called from PreToolUse handler after each
    INSERT to keep the per-session set bounded."""
    # Two-step: find the id boundary, then delete anything with smaller ts.
    # Cheaper than ROW_NUMBER() on SQLite which doesn't ship it.
    q = (
        select(SessionFileTouch.id, SessionFileTouch.ts)
        .where(SessionFileTouch.sid == sid)
        .order_by(SessionFileTouch.ts.desc(), SessionFileTouch.id.desc())
        .limit(keep)
    )
    keep_ids: list[int] = [row[0] for row in (await db.execute(q)).all()]
    if not keep_ids:
        return 0
    del_stmt = (
        delete(SessionFileTouch)
        .where(SessionFileTouch.sid == sid, ~SessionFileTouch.id.in_(keep_ids))
    )
    res = await db.execute(del_stmt)
    return res.rowcount or 0
