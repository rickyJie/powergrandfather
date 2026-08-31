"""Resolve an agent's `prompt_source` to a concrete prompt body.

Supports two forms:
  - http(s) URL  → httpx.get with 10s timeout
  - absolute path → Path.read_text()

Relative paths are rejected to avoid ambiguity (which cwd?). The resolver runs
once at create-time (and again on explicit `refresh_from_source`); the resolved
body is then stored in `prompt_cached` and that is what gets injected at every
spawn.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 10.0
_MAX_BYTES = 1 * 1024 * 1024  # 1 MB — a system prompt past this is almost certainly a mistake


class PromptLoadError(Exception):
    pass


async def load_prompt(source: str) -> str:
    """Resolve `source` to a prompt body string.

    Raises `PromptLoadError` with a human-readable detail on failure (404,
    bad path, oversize, decode error).
    """
    if not source or not source.strip():
        raise PromptLoadError("source is empty")
    s = source.strip()
    if s.startswith(("http://", "https://")):
        return await _load_url(s)
    return _load_path(s)


async def _load_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SEC, follow_redirects=True) as cli:
            r = await cli.get(url)
    except httpx.HTTPError as e:
        raise PromptLoadError(f"http fetch failed: {e}") from e
    if r.status_code >= 400:
        raise PromptLoadError(f"http {r.status_code} from {url}")
    body = r.content
    if len(body) > _MAX_BYTES:
        raise PromptLoadError(
            f"prompt body too large: {len(body)} bytes > {_MAX_BYTES}"
        )
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PromptLoadError(f"prompt is not valid utf-8: {e}") from e


def _load_path(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        raise PromptLoadError(f"path must be absolute, got: {path!r}")
    if not p.exists():
        raise PromptLoadError(f"path does not exist: {path!r}")
    if not p.is_file():
        raise PromptLoadError(f"path is not a regular file: {path!r}")
    if p.stat().st_size > _MAX_BYTES:
        raise PromptLoadError(
            f"prompt file too large: {p.stat().st_size} bytes > {_MAX_BYTES}"
        )
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise PromptLoadError(f"file is not valid utf-8: {e}") from e
