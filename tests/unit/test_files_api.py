"""Happy-path + generic-error coverage for `/api/files/*` (N1, slot 13).

Covers the four endpoints in `csm.api.files` — `preview`, `raw`,
`oss-redirect`, and `recent/{sid}` — plus the internal helper
`prune_session_file_touches`. Complementary to (and non-overlapping with)
`test_files_oss_redirect.py` (C4 key-regex / phishing surface) and
`test_files_allowlist.py` (C6 opt-in root allowlist). This module
deliberately does NOT re-test those security cases.

DB fixture pattern follows `tests/integration/test_hooks_endpoint.py`:
in-memory-ish tmp sqlite + hand-mounted `app.state.sessionmaker` so the
`Depends(get_db_sessionmaker)` in `recent()` resolves without spinning
the whole lifespan.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from csm.api.files import prune_session_file_touches
from csm.api.files import router as files_router
from csm.config import settings
from csm.models import Base, SessionFileTouch
from csm.models.session import Session, SessionStatus, SessionType
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ---------- fixtures ----------

@pytest_asyncio.fixture
async def db_sm():
    """Fresh sqlite per test — cheap enough and avoids cross-test bleed."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest_asyncio.fixture
async def client(db_sm):
    """FastAPI app with just the files router + the sessionmaker wired in."""
    app = FastAPI()
    app.include_router(files_router)
    app.state.sessionmaker = db_sm
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_session(sm, sid: str = "sid-files-1", cwd: str = "/tmp") -> str:
    """recent/{sid} query itself doesn't join Session, but seeding a parent
    row keeps future FK tightening safe. `cwd` is also read by the preview
    endpoint when a relative path is passed alongside session_id."""
    async with sm() as s:
        s.add(Session(
            id=sid,
            cwd=cwd,
            type=SessionType.INTERACTIVE,
            status=SessionStatus.STARTING,
        ))
        await s.commit()
        return sid


async def _seed_touch(sm, sid: str, path: str, tool: str, ts: datetime) -> None:
    async with sm() as s:
        s.add(SessionFileTouch(sid=sid, path=path, tool=tool, ts=ts))
        await s.commit()


# ---------- /preview ----------

@pytest.mark.asyncio
async def test_preview_plain_text_happy_path(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "hello.txt"
    f.write_text("hello world\nsecond line\n")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    # pygments-highlighted HTML — the source text must survive somewhere.
    assert "hello world" in r.text
    assert "<html" in r.text.lower()


@pytest.mark.asyncio
async def test_preview_markdown_rendered(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "doc.md"
    f.write_text("# Heading\n\nsome text with `inline code`.\n")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    # The MD branch emits both a rendered pane and a source pane, plus
    # the source/rendered tab controls unique to the .md path.
    assert "<h1" in r.text  # rendered heading
    assert "<code>inline code</code>" in r.text
    assert 'data-view="rendered"' in r.text


@pytest.mark.asyncio
async def test_html_preview_uses_higher_size_cap(client, tmp_path, monkeypatch):
    """HTML files get a 10 MiB cap (vs the 2 MiB generic text cap) so
    3-8 MB data-viz reports render inline instead of forcing a download.
    Repro: bump `file_preview_max_bytes` down to 100 KB, verify a
    200 KB HTML still previews while a 200 KB python file falls to the
    'too large' card."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    monkeypatch.setattr(settings, "file_preview_max_bytes", 100 * 1024)  # 100 KiB
    monkeypatch.setattr(settings, "file_preview_html_max_bytes", 10 * 1024 * 1024)

    html = tmp_path / "big.html"
    html.write_text("<html><body>" + ("<p>x</p>" * 30000) + "</body></html>")  # ~240 KB
    r = await client.get("/api/files/preview", params={"path": str(html)})
    assert r.status_code == 200, r.text
    assert "File too large" not in r.text, "HTML under html-cap must render inline"
    # HTML branch is identified by the iframe + Source/Rendered tabs
    assert "iframe" in r.text

    py = tmp_path / "big.py"
    py.write_text("x = 1\n" * 30000)  # ~180 KB, over the 100 KB text cap
    r2 = await client.get("/api/files/preview", params={"path": str(py)})
    assert r2.status_code == 200, r2.text
    assert "File too large" in r2.text, "non-HTML must still respect the text cap"


@pytest.mark.asyncio
async def test_html_preview_over_html_cap_returns_download_card(client, tmp_path, monkeypatch):
    """An HTML file larger than `file_preview_html_max_bytes` gets the
    same 'too large' download card as other over-cap files (the higher
    limit doesn't mean 'never gate')."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    monkeypatch.setattr(settings, "file_preview_html_max_bytes", 50 * 1024)  # 50 KiB
    html = tmp_path / "huge.html"
    html.write_text("<html>" + ("x" * 100_000) + "</html>")  # 100 KB, over 50 KB cap
    r = await client.get("/api/files/preview", params={"path": str(html)})
    assert r.status_code == 200
    assert "File too large" in r.text
    # Cap message should reflect the HTML-specific cap, not the generic one
    assert "50" in r.text  # 50 KB or similar rendering


@pytest.mark.asyncio
async def test_html_preview_large_skips_source_highlight(client, tmp_path, monkeypatch):
    """When an HTML file exceeds the generic text cap (but is still under
    the html cap), the Source tab falls back to raw `<pre>` instead of
    invoking pygments — highlighting multi-MB HTML is slow AND balloons
    the response. Rendered tab still works via the iframe."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    monkeypatch.setattr(settings, "file_preview_max_bytes", 10 * 1024)  # 10 KiB — tiny
    monkeypatch.setattr(settings, "file_preview_html_max_bytes", 10 * 1024 * 1024)
    html = tmp_path / "midsize.html"
    html.write_text("<html><body>" + ("<span>hi</span>" * 5000) + "</body></html>")  # ~70 KB
    r = await client.get("/api/files/preview", params={"path": str(html)})
    assert r.status_code == 200
    # Iframe (rendered) present regardless of source-tab strategy
    assert "iframe" in r.text
    # No pygments span markup for the source tab body (indicative check —
    # the ".pyghi" wrapper is what the highlighter emits). Instead a
    # plain `<pre>` fallback wraps the escaped content.
    # The Rendered/Source shell + tabs are still emitted, but the source
    # payload itself has no pygments span classes.
    # (Loose check — the shell CSS may still reference `.pyghi` as an
    # unused rule; we're asserting the source body is escaped text, not
    # highlighted HTML.)
    assert "&lt;span&gt;hi&lt;/span&gt;" in r.text


@pytest.mark.asyncio
async def test_preview_yaml_renders_collapsible_tree(client, tmp_path, monkeypatch):
    """`local:5f872dbb` — yaml files render as a structural tree, not as
    plain syntax-highlighted source. Tree view is the default tab; Source
    tab is available as a fallback."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "wf.yaml"
    f.write_text(
        "name: sync\n"
        "stages:\n"
        "  - id: r1\n"
        "    kind: claude\n"
        "  - id: r2\n"
        "    kind: poll\n"
        "enabled: true\n"
        "count: 42\n"
    )
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    # Tree structure primitives that only the yaml branch emits
    assert 'class="yaml-tree"' in r.text
    assert 'class="yk"' in r.text and ">name<" in r.text
    assert 'yb yb-list' in r.text  # size badge on the stages list
    assert '>true<' in r.text and '>42<' in r.text  # bool / num scalars
    # Both tabs present, Tree is default
    assert 'data-view="rendered"' in r.text
    assert 'data-view="source"' in r.text


@pytest.mark.asyncio
async def test_preview_yml_extension_also_rendered(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "config.yml"
    f.write_text("k: v\n")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200
    assert 'class="yaml-tree"' in r.text


@pytest.mark.asyncio
async def test_preview_yaml_parse_error_falls_back_to_source(client, tmp_path, monkeypatch):
    """Malformed yaml: banner in the (hidden) tree tab, Source tab visible
    by default so the user can inspect the offending file directly."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "bad.yaml"
    # Mixed indentation + a stray tab guarantees a parse error
    f.write_text("a:\n  b: 1\n\tc: 2\n")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "YAML parse error" in r.text
    # Language label picks up the warning marker so the header hints at
    # a bad file at a glance
    assert "YAML" in r.text
    # Source tab is the visible one on parse-fail
    assert 'id="v-source"' in r.text


@pytest.mark.asyncio
async def test_preview_context_bar_when_session_id_given(client, tmp_path, monkeypatch):
    """When `session_id` resolves to a real Session row, the preview
    shell injects a context bar with a Back-to-session link and a
    'View diff' counterpart pointing at diff-view."""
    from csm.models import Session as SessRow
    from csm.models.session import SessionStatus, SessionType

    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "ctx.py"
    f.write_text("x = 1\n")

    # Plant a session whose id we can pass — the ASGI test client's
    # sessionmaker fixture is already wired in the conftest.
    sm = client._transport.app.state.sessionmaker
    async with sm() as db:
        row = SessRow(
            cwd=str(tmp_path),
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            title="my-review",
            agent="claude",
        )
        db.add(row)
        await db.commit()
        sid = row.id

    r = await client.get(
        "/api/files/preview",
        params={"path": str(f), "session_id": sid},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "csm-ctx-bar" in body
    assert "Back to session" in body
    assert f"/sessions/{sid}" in body
    assert "my-review" in body
    # Preview mode → counterpart is "View diff"
    assert "View diff" in body
    assert f"/api/sessions/{sid}/changes/diff-view" in body


@pytest.mark.asyncio
async def test_preview_no_context_bar_without_session_id(client, tmp_path, monkeypatch):
    """Plain preview (no session_id) still works and does not inject a
    context bar — keeps the standalone-viewer UX unchanged. The `.csm-ctx-bar`
    CSS block is always present in the shell head; what we assert is that
    the actual `<div class="csm-ctx-bar">` element is NOT emitted."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "plain.py"
    f.write_text("x = 1\n")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200
    assert '<div class="csm-ctx-bar">' not in r.text
    assert "Back to session" not in r.text


@pytest.mark.asyncio
async def test_preview_markdown_math_wired(client, tmp_path, monkeypatch):
    """MD preview injects KaTeX + arithmatex so LaTeX math renders.
    Inline `$E = mc^2$` becomes `<span class="arithmatex">` server-side
    and KaTeX auto-render (loaded from CDN) picks it up client-side."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "math.md"
    f.write_text(
        "Inline: $E = mc^2$\n\n"
        "Display:\n$$\n\\int_0^\\infty e^{-x} dx = 1\n$$\n"
    )
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    # arithmatex wraps the math in <span class="arithmatex">
    assert 'class="arithmatex"' in r.text
    # KaTeX auto-render loader must be present so the browser renders it
    assert "katex.min.css" in r.text
    assert "renderMathInElement" in r.text


@pytest.mark.asyncio
async def test_preview_binary_returns_download_hint(client, tmp_path, monkeypatch):
    """A NUL-containing file trips the `_looks_binary` heuristic and the
    endpoint still returns 200 (HTML) but the body is the download hint
    rather than highlighted source."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02\x03garbage\x00\x00more")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "Binary file" in r.text
    assert "Download raw" in r.text


@pytest.mark.asyncio
async def test_preview_nonexistent_returns_404(client, monkeypatch):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    # Path resolves cleanly (parent /tmp exists) but the file doesn't.
    r = await client.get(
        "/api/files/preview",
        params={"path": "/tmp/definitely-not-there-xyz-6f2c1a.txt"},
    )
    assert r.status_code == 404, r.text
    assert "not found" in r.text.lower()


@pytest.mark.asyncio
async def test_preview_size_cap_returns_download_link(
    client, tmp_path, monkeypatch
):
    """Files past `file_preview_max_bytes` short-circuit to the download-only
    HTML pane instead of loading megabytes into memory."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    monkeypatch.setattr(settings, "file_preview_max_bytes", 16)  # cap tiny
    f = tmp_path / "big.txt"
    f.write_text("A" * 128)  # 128 B > 16 B cap
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "over the" in r.text and "preview cap" in r.text
    # And the download link back to /raw is present.
    assert "/api/files/raw" in r.text


# ---------- /raw ----------

@pytest.mark.asyncio
async def test_raw_happy_path(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "payload.json"
    body = b'{"k":"v"}'
    f.write_bytes(body)
    r = await client.get("/api/files/raw", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert r.content == body
    # mimetypes should have guessed application/json.
    assert "json" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_raw_nonexistent_returns_404(client, monkeypatch):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    r = await client.get(
        "/api/files/raw",
        params={"path": "/tmp/definitely-not-there-xyz-6f2c1a.bin"},
    )
    assert r.status_code == 404, r.text


# ---------- /recent/{sid} ----------

@pytest.mark.asyncio
async def test_recent_empty_returns_empty_list(client, db_sm):
    await _seed_session(db_sm, sid="sid-empty")
    r = await client.get("/api/files/recent/sid-empty")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"count": 0, "items": []}


@pytest.mark.asyncio
async def test_recent_after_touches_returns_ordered(client, db_sm):
    sid = await _seed_session(db_sm, sid="sid-three")
    base = datetime(2026, 7, 25, 10, 0, 0)
    # Insert out of order to prove the endpoint (not insert order) sorts.
    await _seed_touch(db_sm, sid, "/tmp/b.py", "Write", base + timedelta(minutes=1))
    await _seed_touch(db_sm, sid, "/tmp/a.py", "Edit", base + timedelta(minutes=2))
    await _seed_touch(db_sm, sid, "/tmp/c.py", "MultiEdit", base)

    r = await client.get("/api/files/recent/sid-three")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    paths = [item["path"] for item in body["items"]]
    # Newest first: a.py (base+2) → b.py (base+1) → c.py (base).
    assert paths == ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"]
    # Shape check on one row.
    first = body["items"][0]
    assert set(first.keys()) == {"id", "path", "tool", "ts"}
    assert first["tool"] == "Edit"


# ---------- html preview rendered/source (local:f7d02a63) ----------

@pytest.mark.asyncio
async def test_preview_html_uses_sandboxed_iframe(client, tmp_path, monkeypatch):
    """HTML files render inside a scripts-only sandboxed iframe.

    Plotly / Chart.js / vanilla-JS reports need scripts to render, so
    the iframe uses ``sandbox="allow-scripts"``. Same-origin is still
    withheld, so scripts cannot read this document's cookies /
    localStorage or hit ``/api/*`` with the user's session. The iframe
    also gets ``allowfullscreen`` so the toolbar Fullscreen button works.
    """
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "report.html"
    f.write_text("<html><body><h1>Hello</h1><script>alert(1)</script></body></html>")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "<iframe" in r.text
    assert 'sandbox="allow-scripts"' in r.text
    assert "allowfullscreen" in r.text
    # Iframe src must point at the /inline directory-mounted route (NOT
    # /raw?path=), so relative sub-resources (<video>, <img>, <script>,
    # <link>) resolve against sibling files in the source file's parent
    # dir. This is the fix for the "video won't play" regression.
    assert "/api/files/inline/" in r.text
    assert "/api/files/raw?path=" in r.text  # Open raw ↗ escape hatch still present
    # Toolbar shape (redesigned — no more Rendered/Source tabs):
    assert 'id="html-tool-source"' in r.text
    assert 'id="html-tool-fullscreen"' in r.text
    assert 'Open raw' in r.text


@pytest.mark.asyncio
async def test_preview_htm_ext_also_treated_as_html(client, tmp_path, monkeypatch):
    """Legacy .htm extension follows the same HTML branch."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "legacy.htm"
    f.write_text("<p>tiny</p>")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "<iframe" in r.text and 'sandbox="allow-scripts"' in r.text
    assert "/api/files/inline/" in r.text


@pytest.mark.asyncio
async def test_inline_serves_sibling_resource(client, tmp_path, monkeypatch):
    """/api/files/inline/{b64}/{filename} serves any file under the b64
    directory — the mechanism that lets an HTML preview's `<video
    src="./movie.mp4">` reach the sibling video."""
    from csm.api.files import _encode_dir_b64

    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    html = tmp_path / "index.html"
    html.write_text(
        '<video src="./movie.mp4" controls></video>'
        '<img src="./thumb.png"><link rel="stylesheet" href="./style.css">'
    )
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake mp4 bytes for the test")
    b64 = _encode_dir_b64(tmp_path)

    r_html = await client.get(f"/api/files/inline/{b64}/index.html")
    assert r_html.status_code == 200
    assert r_html.headers["content-type"].startswith("text/html")
    assert 'src="./movie.mp4"' in r_html.text

    r_vid = await client.get(f"/api/files/inline/{b64}/movie.mp4")
    assert r_vid.status_code == 200
    assert r_vid.content == b"fake mp4 bytes for the test"
    # inline disposition — browsers must render (or play) the response,
    # not offer it as a download.
    assert "inline" in r_vid.headers.get("content-disposition", "").lower()


@pytest.mark.asyncio
async def test_inline_rejects_dotdot_escape(client, tmp_path, monkeypatch):
    """`..` in the filename segment must never let a caller read files
    outside the b64-encoded base directory — the sole security boundary
    for /inline."""
    from csm.api.files import _encode_dir_b64

    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    sub = tmp_path / "inside"
    sub.mkdir()
    (sub / "index.html").write_text("<p>ok</p>")
    (tmp_path / "SECRET.txt").write_text("do not leak")
    b64 = _encode_dir_b64(sub)

    # URL-encoded `..` so httpx / starlette don't collapse the segment
    # before it reaches our route handler.
    r = await client.get(
        f"/api/files/inline/{b64}/%2E%2E/SECRET.txt",
        follow_redirects=False,
    )
    # Must be a hard rejection — never a 200 with secret bytes.
    assert r.status_code in (400, 403, 404)
    assert b"do not leak" not in r.content


@pytest.mark.asyncio
async def test_inline_rejects_garbage_b64(client, monkeypatch):
    """Non-b64 dir segment → 400, not 500."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    r = await client.get("/api/files/inline/!!!not_base64!!!/foo.html")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_inline_respects_allowed_roots(client, tmp_path, monkeypatch):
    """When file_preview_allowed_roots is set, /inline must refuse
    directories outside those roots — matches /preview and /raw policy."""
    from csm.api.files import _encode_dir_b64

    other = tmp_path / "outside"
    other.mkdir()
    (other / "x.html").write_text("<p>x</p>")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [str(allowed)])

    b64 = _encode_dir_b64(other)
    r = await client.get(f"/api/files/inline/{b64}/x.html")
    assert r.status_code == 403


# ---------- relative path + session_id (local:617d9bab) ----------

@pytest.mark.asyncio
async def test_preview_relative_path_resolves_against_session_cwd(
    client, db_sm, tmp_path, monkeypatch
):
    """A path with no leading `/` or `~` is resolved against Session.cwd
    when a matching session_id query parameter is passed."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    (tmp_path / "hello.txt").write_text("hello via cwd\n")
    await _seed_session(db_sm, sid="sid-rel-happy", cwd=str(tmp_path))
    r = await client.get(
        "/api/files/preview",
        params={"path": "hello.txt", "session_id": "sid-rel-happy"},
    )
    assert r.status_code == 200, r.text
    assert "hello via cwd" in r.text


@pytest.mark.asyncio
async def test_preview_relative_path_traversal_is_blocked(
    client, db_sm, tmp_path, monkeypatch
):
    """`../` escapes must 403 even when a valid session_id anchors the
    resolve. This is the sole surface where the caller can drive path
    resolution off a variable prefix, so the containment check matters."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    await _seed_session(db_sm, sid="sid-rel-esc", cwd=str(inner))
    r = await client.get(
        "/api/files/preview",
        params={"path": f"../{outside.name}", "session_id": "sid-rel-esc"},
    )
    assert r.status_code == 403, r.text
    assert "escapes session cwd" in r.text
    try:
        outside.unlink()
    except OSError:
        pass


@pytest.mark.asyncio
async def test_preview_relative_path_without_session_id_falls_back_to_cwd_resolve(
    client, tmp_path, monkeypatch
):
    """Legacy callers that pass a relative path with no session_id keep
    the old behavior: resolves against the uvicorn cwd via _resolve.
    In practice that path won't exist so we assert 404, not 400."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    r = await client.get(
        "/api/files/preview",
        params={"path": "some_nonexistent_relative_thing_9f2c.txt"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_preview_relative_path_unknown_session_returns_404(
    client, db_sm, monkeypatch
):
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    r = await client.get(
        "/api/files/preview",
        params={"path": "foo.txt", "session_id": "no-such-sid"},
    )
    assert r.status_code == 404, r.text
    assert "no-such-sid" in r.text


# ---------- s3:// pass-through in /preview (today's fix) ----------

@pytest.mark.asyncio
async def test_preview_s3_uri_302s_to_oss_redirect(client, monkeypatch):
    """`/api/files/preview?path=s3://bucket/key` → 302 through oss-redirect.
    Fix rationale: browser paste of s3:// belongs on OSS, not local FS lookup.
    """
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    r = await client.get(
        "/api/files/preview",
        params={"path": "s3://example-bucket/PowerGrandFather/guide.md"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert loc.startswith("/api/files/oss-redirect?uri=")
    # The re-encoded uri must round-trip the original s3:// prefix.
    assert "s3%3A%2F%2Fexample-bucket" in loc


@pytest.mark.asyncio
async def test_preview_double_slash_treated_as_s3(client, monkeypatch):
    """Browsers strip `s3:` from address-bar `s3://…` because they parse
    it as a bare scheme. The residue `//bucket/key` must still resolve to
    the OSS redirect, not a local `/bucket/key` file lookup."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    r = await client.get(
        "/api/files/preview",
        params={"path": "//example-bucket/PowerGrandFather/guide.md"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert loc.startswith("/api/files/oss-redirect?uri=")
    # Prefix must be reassembled to s3:// so the redirect target parses.
    assert "s3%3A%2F%2Fexample-bucket" in loc


@pytest.mark.asyncio
async def test_preview_local_file_still_renders(client, tmp_path, monkeypatch):
    """The s3:// short-circuit must NOT swallow normal local paths — those
    still render as HTML preview. Regression guard for the branch order in
    `preview()`."""
    monkeypatch.setattr(settings, "file_preview_allowed_roots", [])
    f = tmp_path / "keep.py"
    f.write_text("print('hi')\n")
    r = await client.get("/api/files/preview", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "hi" in r.text
    assert "<html" in r.text.lower()


# ---------- shell helpers (Phase A beautification) ----------

def test_build_breadcrumb_short_absolute_path_not_folded():
    """Short paths (≤ 5 middle segments + filename) render every segment;
    last one gets `seg-last` for bold weight in the header."""
    from csm.api.files import _build_breadcrumb
    html = _build_breadcrumb("/a/b/c/d.py")
    assert 'class="seg seg-last"' in html
    assert "d.py" in html
    # 3 middle segs + 1 last seg = 4 spans, no ellipsis.
    assert "seg-ellipsis" not in html


def test_build_breadcrumb_long_path_folds_middle():
    """Deep paths collapse the middle into a `…` marker so the header
    actions stay on one line. Head keeps first 2 segs; tail keeps last 2."""
    from csm.api.files import _build_breadcrumb
    html = _build_breadcrumb("/home/user/repo/backend/csm/utils/time.py")
    assert 'class="seg-ellipsis"' in html
    # Elided middle preserved as a tooltip for hover discovery.
    assert 'title="repo/backend/csm"' in html
    # Last segment still bold + present.
    assert 'class="seg seg-last"' in html
    assert "time.py" in html
    # Head shows first two segs.
    assert 'class="seg">home<' in html
    assert 'class="seg">user<' in html


def test_build_breadcrumb_tilde_prefix():
    """`~/foo/bar.txt` preserves `~` as a distinct root segment."""
    from csm.api.files import _build_breadcrumb
    html = _build_breadcrumb("~/foo/bar.txt")
    assert ">~<" in html or "~</span>" in html
    assert "bar.txt" in html
    assert 'class="seg seg-last"' in html


def test_build_breadcrumb_empty_string_safe():
    """Empty input must not crash — the header still needs to render."""
    from csm.api.files import _build_breadcrumb
    assert _build_breadcrumb("") == ""


def test_dual_pygments_css_has_both_schemes():
    """`_dual_pygments_css` scopes light + dark under `prefers-color-scheme`
    so token colors track the shell theme without JS toggles."""
    from csm.api.files import _dual_pygments_css
    css = _dual_pygments_css("pyghi")
    assert "@media (prefers-color-scheme: light)" in css
    assert "@media (prefers-color-scheme: dark)" in css
    # Both stylesheets scope everything to the `.pyghi` cssclass.
    assert ".pyghi" in css


# ---------- prune_session_file_touches (internal helper) ----------

@pytest.mark.asyncio
async def test_prune_keeps_last_100(db_sm):
    """Seed 105 touches, prune to keep 100 → 5 deleted, oldest gone."""
    sid = await _seed_session(db_sm, sid="sid-prune")
    base = datetime(2026, 7, 25, 0, 0, 0)
    async with db_sm() as s:
        for i in range(105):
            s.add(SessionFileTouch(
                sid=sid,
                path=f"/tmp/f{i:03d}.txt",
                tool="Write",
                ts=base + timedelta(seconds=i),
            ))
        await s.commit()

    async with db_sm() as s:
        deleted = await prune_session_file_touches(s, sid, keep=100)
        await s.commit()
    assert deleted == 5

    # Verify the surviving set is the newest 100 (paths f005 .. f104).
    async with db_sm() as s:
        from sqlalchemy import select
        rows = (await s.execute(
            select(SessionFileTouch.path)
            .where(SessionFileTouch.sid == sid)
            .order_by(SessionFileTouch.ts.asc())
        )).scalars().all()
    assert len(rows) == 100
    assert rows[0] == "/tmp/f005.txt"
    assert rows[-1] == "/tmp/f104.txt"
