"""Unit tests for the codex /status pane parser.

Independent of pexpect / real subprocess — feeds captured pane strings
to `_parse_pane` and asserts the three extracted values (week_pct_used,
week_reset, tier).
"""
from __future__ import annotations

from csm.modules.token.codex_usage_probe import _parse_pane

# Real /status pane captured from codex 0.145.0 on 2026-07-26. Contains
# both the Account line (with tier) and the Weekly limit line. Widths
# and box-drawing chars kept intact so the parser regex is exercised on
# realistic input.
_REAL_STATUS_PANE = """
/status

╭─────────────────────────────────────────────────────────────────────────────────╮
│  >_ OpenAI Codex (v0.145.0)                                                     │
│                                                                                 │
│ Visit https://chatgpt.com/codex/settings/usage for up-to-date                   │
│ information on rate limits and credits                                          │
│                                                                                 │
│  Model:                gpt-5.6-sol (reasoning low, summaries auto)              │
│  Directory:            /tmp                                                     │
│  Permissions:          Full Access                                              │
│  Agents.md:            <none>                                                   │
│  Account:              owner@example.com (Plus)               │
│  Collaboration mode:   Default                                                  │
│  Session:              019f9d0d-2835-7d42-a1eb-83fb57b88952                     │
│                                                                                 │
│  Weekly limit:         [████████████████████] 100% left (resets 14:11 on 2 Aug) │
╰─────────────────────────────────────────────────────────────────────────────────╯
"""


def test_parse_pane_real_capture():
    week_pct, week_reset, tier = _parse_pane(_REAL_STATUS_PANE)
    # 100% left → 0% used
    assert week_pct == 0
    assert week_reset == "14:11 on 2 Aug"
    assert tier == "Plus"


def test_parse_pane_partial_used():
    pane = "Weekly limit: [████░░░░░░░░░░░░] 40% left (resets 09:00 on 12 Sep)"
    week_pct, week_reset, tier = _parse_pane(pane)
    assert week_pct == 60  # 100 - 40
    assert week_reset == "09:00 on 12 Sep"
    assert tier is None  # no Account line in this fixture


def test_parse_pane_zero_pct_left():
    """When quota is fully spent, codex renders 0% left."""
    pane = "Weekly limit: [░░░░░░░░░░░░░░░░░░░░] 0% left (resets 14:00 on 5 Aug)"
    week_pct, _, _ = _parse_pane(pane)
    assert week_pct == 100


def test_parse_pane_over_100_clamps():
    """Codex has been seen to render 101% during a refresh — clamp to 100."""
    pane = "Weekly limit: [X] 101% left (resets 12:00 on 1 Sep)"
    week_pct, _, _ = _parse_pane(pane)
    assert week_pct == 0  # 101 clamped to 100, then 100-100=0


def test_parse_pane_missing_reset_still_gets_pct():
    """Reset clause missing shouldn't kill the pct parse."""
    pane = "Weekly limit: [████] 75% left"
    week_pct, week_reset, _ = _parse_pane(pane)
    assert week_pct == 25
    assert week_reset is None


def test_parse_pane_missing_weekly_line():
    """Reset text present elsewhere shouldn't be misattributed to weekly."""
    pane = (
        " Account:  x@y.com (Pro)\n"
        " resets any time you talk to support\n"
    )
    week_pct, week_reset, tier = _parse_pane(pane)
    assert week_pct is None
    assert week_reset is None
    assert tier == "Pro"


def test_parse_pane_tier_variants():
    """Codex tier names include Plus / Pro / Team / Enterprise."""
    for name in ("Plus", "Pro", "Team", "Enterprise"):
        pane = f"Account:  user@x.com ({name})\nWeekly limit: [] 50% left (resets tomorrow)"
        _, _, tier = _parse_pane(pane)
        assert tier == name, f"expected {name} but got {tier}"


def test_parse_pane_case_insensitive_labels():
    pane = (
        "account:  user@example.com (Plus)\n"
        "weekly limit: [] 30% left (resets 08:00 on 3 Aug)\n"
    )
    week_pct, week_reset, tier = _parse_pane(pane)
    assert week_pct == 70
    assert week_reset == "08:00 on 3 Aug"
    assert tier == "Plus"


def test_parse_pane_empty_returns_all_none():
    week_pct, week_reset, tier = _parse_pane("some unrelated text")
    assert (week_pct, week_reset, tier) == (None, None, None)


def test_parse_pane_refresh_marker_returns_none():
    """First /status call: 'Limits: refresh requested; run /status again shortly.'
    Parser should return (None, None, None) so the caller can detect the
    refresh-not-ready case."""
    pane = (
        " Account:  user@x.com (Plus)\n"
        " Limits:   refresh requested; run /status again shortly.\n"
    )
    week_pct, week_reset, tier = _parse_pane(pane)
    assert week_pct is None
    assert week_reset is None
    assert tier == "Plus"
