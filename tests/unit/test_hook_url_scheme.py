"""Regression: hook URL scheme must match uvicorn TLS state.

Root cause of "sessions stuck at RUNNING" (2026-07-25): main.py hardcoded
`http://127.0.0.1:...` as the hook base URL. When uvicorn is booted with
`--ssl-keyfile / --ssl-certfile`, the CSM API listens on HTTPS, so the
python `-c` hook POST to plain http gets "Empty reply from server" and the
except-swallow at the tail silently drops it. Stop hook never lands →
`SessionStatus.RUNNING` never transitions to `IDLE`.
"""
from __future__ import annotations

# Targets the LIVE builder. These assertions previously imported
# `manager._build_hook_py_code`, a byte-identical duplicate whose last
# production caller was deleted with the legacy spawn branch — so the
# shell-quoting and ssl-context checks below were guarding a copy nothing
# ran. Breaking the real one left them green.
from csm.backends.claude.hooks import build_hook_py_code


def test_http_url_hook_snippet_does_not_import_ssl() -> None:
    code = build_hook_py_code("http://127.0.0.1:8000/api/hooks/abc")
    # Byte-parity with the pre-TLS version so plain-http deployments are
    # unchanged. If someone adds `import ssl` here unconditionally the
    # untouched code path grows a dependency, which we don't want.
    assert "import ssl" not in code
    assert "context=" not in code
    assert "'http://127.0.0.1:8000/api/hooks/abc'" in code


def test_https_url_hook_snippet_uses_unverified_context() -> None:
    code = build_hook_py_code("https://127.0.0.1:8000/api/hooks/abc")
    # Self-signed cert lives in secrets/csm-cert.pem; verification would
    # fail. Only loopback CSM ever sees this URL so MITM is not a threat.
    assert ", ssl," in code  # imported alongside sys/urllib
    assert "ssl._create_unverified_context()" in code
    assert "context=" in code
    assert "'https://127.0.0.1:8000/api/hooks/abc'" in code


def test_hook_snippet_wraps_urlopen_in_try_except() -> None:
    # Hook contract: never propagate an error back to claude — a non-200
    # response blocks the session waiting for retry (see hooks.py docstring).
    for url in (
        "http://127.0.0.1:8000/api/hooks/sid1",
        "https://127.0.0.1:8000/api/hooks/sid2",
    ):
        code = build_hook_py_code(url)
        assert "try:" in code
        assert "except Exception: pass" in code


def test_hook_url_quoting_escapes_hostile_chars() -> None:
    # sid is a uuid so this is defensive, but repr() guarantees the URL
    # cannot break out of the string literal even if a caller ever passes
    # something exotic. If someone switches to f-string interpolation this
    # test will fail — that's the point.
    code = build_hook_py_code("https://127.0.0.1:8000/api/hooks/a'b\"c")
    assert 'u.Request(' in code
    # repr keeps everything inside a single Python string literal
    assert "'https://127.0.0.1:8000/api/hooks/a\\'b\"c'" in code or \
           '"https://127.0.0.1:8000/api/hooks/a\'b\\"c"' in code
