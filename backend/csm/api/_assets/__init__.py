"""Static CSS/JS served inside the server-rendered HTML pages.

These pages (`/api/sessions/{sid}/changes/diff-view`, `/api/files/preview`) are
plain server-rendered HTML — they deliberately do NOT go through the Vue build,
because they must be openable as a bare URL with no SPA loaded.

That reason justifies "no bundler", not "keep it in a Python string". Held as
`.py` string literals, ~1200 lines of CSS and JS got no syntax highlighting, no
linting, no editor navigation, and were invisible to every refactoring tool —
which is how `api/files.py` ended up 50% non-Python by line count.

Assets are read once and cached; they are shipped as package data (see
`[tool.setuptools.package-data]` in pyproject.toml), so this works from a
wheel as well as from an editable install.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

__all__ = ["asset", "script_tag"]


@cache
def asset(name: str) -> str:
    """Contents of `_assets/<name>`. Cached — these never change at runtime."""
    return files(__package__).joinpath(name).read_text(encoding="utf-8")


def script_tag(name: str) -> str:
    """`<script>` wrapping a `.js` asset. The asset itself holds no tags, so
    editors and linters see it as the JavaScript it is.

    The asset's own trailing newline is normalized away first, so the output is
    byte-identical to the inline literal this replaced — a file that ends with
    a newline (every well-formed one does) would otherwise gain a blank line.
    """
    return f"<script>\n{asset(name).rstrip(chr(10))}\n</script>"


# No `style_tag` counterpart: both CSS assets are spliced into an existing
# <style> block (`preview_head.html`) or appended to another stylesheet
# (`diff_css`), so neither wants its own tag. Add one when something does.
