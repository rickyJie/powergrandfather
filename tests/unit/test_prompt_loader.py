"""Unit tests for prompt_loader: file vs URL resolution + error mapping."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from csm.modules.agent.prompt_loader import PromptLoadError, load_prompt


async def test_load_path_basic(tmp_path: Path):
    p = tmp_path / "sys.md"
    p.write_text("you are a reviewer", encoding="utf-8")
    out = await load_prompt(str(p))
    assert out == "you are a reviewer"


async def test_load_path_rejects_relative():
    with pytest.raises(PromptLoadError, match="absolute"):
        await load_prompt("relative/path.md")


async def test_load_path_rejects_missing(tmp_path: Path):
    p = tmp_path / "nope.md"
    with pytest.raises(PromptLoadError, match="does not exist"):
        await load_prompt(str(p))


async def test_load_path_rejects_directory(tmp_path: Path):
    with pytest.raises(PromptLoadError, match="not a regular file"):
        await load_prompt(str(tmp_path))


async def test_load_path_rejects_empty():
    with pytest.raises(PromptLoadError, match="empty"):
        await load_prompt("")
    with pytest.raises(PromptLoadError, match="empty"):
        await load_prompt("   ")


async def test_load_url_ok(monkeypatch):
    async def fake_get(self, url, **kw):
        return httpx.Response(200, content=b"system prompt body")
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    out = await load_prompt("https://example.com/p.md")
    assert out == "system prompt body"


async def test_load_url_404(monkeypatch):
    async def fake_get(self, url, **kw):
        return httpx.Response(404, content=b"")
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(PromptLoadError, match="http 404"):
        await load_prompt("https://example.com/p.md")


async def test_load_url_transport_error(monkeypatch):
    async def fake_get(self, url, **kw):
        raise httpx.ConnectError("boom")
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(PromptLoadError, match="http fetch failed"):
        await load_prompt("https://example.com/p.md")


async def test_load_path_rejects_oversize(tmp_path: Path, monkeypatch):
    # Patch the constant to a tiny limit so we don't need an MB file.
    from csm.modules.agent import prompt_loader
    monkeypatch.setattr(prompt_loader, "_MAX_BYTES", 16)
    p = tmp_path / "big.md"
    p.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(PromptLoadError, match="too large"):
        await load_prompt(str(p))
