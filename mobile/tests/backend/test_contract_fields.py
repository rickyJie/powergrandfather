"""Contract drift guard for the mobile client.

The mobile UI is a fully self-contained frontend that talks to the SAME backend
as the desktop UI (it no longer imports any desktop TS source). That decoupling
removes the compile-time coupling but reintroduces the risk the whole P0/P1 wave
came from: the mobile api clients hand-mirror the backend contract, so a backend
FIELD RENAME can silently break mobile.

This test pins that contract WITHOUT standing up the full app (its lifespan tails
JSONL + starts schedulers — too heavy / flaky for a unit guard). Instead it
imports the *pure* backend definitions the wire shape derives from:

  - ORM column names   → response field names for DB-backed endpoints
  - request Pydantic models → the bodies the mobile client POSTs / PUTs

If the backend renames a column or a request field the mobile client depends on,
the matching assertion fails and points at the exact drift.

NOT covered here (computed aggregator dicts, no ORM/model to introspect):
Tokens (`/api/tokens/*`), Worktime (`/api/worktime/live`), Budgets status
(`/api/budgets/status`). Those are guarded on the mobile side by the api-client
normalization unit tests (see mobile/tests/frontend). Keep both in sync.
"""

from __future__ import annotations

import pytest

# Response side: fields the mobile client READS must be real ORM columns.
# Request side: bodies it SENDS must be valid model fields.
from csm.api.lark_settings import LarkSettingsPatch, LarkSettingsView
from csm.api.preferences import PreferencePatch
from csm.api.sessions import CreateSessionBody, _SessionMessageBody
from csm.models import UsageSnapshot
from csm.models.notification import Notification
from csm.models.run import Run
from csm.models.session import Session

# Ports and feedback are optional modules — they exist in some builds and not
# others. A module-level `from csm.models.port import ...` therefore took the
# ENTIRE file down with a collection error wherever they're absent, silently
# dropping the seven contracts that have nothing to do with either. Import them
# defensively and let the contract table skip only its own rows.
try:
    from csm.models.port import PortRegistration
except ImportError:  # pragma: no cover - build without the ports module
    PortRegistration = None
try:
    from csm.api.feedback import CreateRequest as FeedbackCreateRequest
    from csm.models.feedback import Feedback
except ImportError:  # pragma: no cover - build without the feedback module
    Feedback = None
    FeedbackCreateRequest = None

try:
    from csm.models.schedule_entry import ScheduleEntry
except Exception:  # pragma: no cover - name/location fallback
    from csm.models import ScheduleEntry


def _columns(model) -> set[str]:
    return set(model.__table__.columns.keys())


def _fields(model) -> set[str]:
    return set(model.model_fields.keys())


# Each entry: (label, actual-name-set, mobile-expected-subset)
ORM_CONTRACTS = [
    (
        "notifications",
        _columns(Notification),
        # mobile/frontend/src/api/notifications.ts RawNotification
        {
            "id",
            "type",
            "session_id",
            "title",
            "body",
            "created_at",
            "read_at",
            "dismissed_at",
            "metadata",
        },
    ),
    (
        "runs",
        _columns(Run),
        # mobile/frontend/src/api/runs.ts StageRun
        {
            "id",
            "mission_id",
            "stage_name",
            "schedule_entry_id",
            "session_id",
            "status",
            "started_at",
            "ended_at",
            "exit_code",
            "parameters",
            "review_note",
        },
    ),
    (
        "schedules",
        _columns(ScheduleEntry),
        # mobile/frontend/src/api/schedules.ts Schedule (ORM-backed subset;
        # `kind` is a computed field, not a column)
        {
            "id",
            "workflow_def_id",
            "cron",
            "run_at",
            "enabled",
            "parameters",
            "next_run_at",
            "last_run_at",
        },
    ),
    (
        "usage-live",
        _columns(UsageSnapshot),
        # mobile/frontend/src/api/tokens.ts UsageSnapshot
        {
            "agent",
            "session_pct",
            "session_reset",
            "week_pct",
            "week_reset",
            "tier",
            "subscription_type",
            "source",
            "duration_ms",
            "error",
            "ts",
        },
    ),
    (
        "sessions",
        _columns(Session),
        # mobile/frontend/src/api/sessions.ts SessionRow (ORM-backed subset;
        # claude_session_id / jsonl_present are computed, not columns)
        {
            "id",
            "title",
            "type",
            "cwd",
            "status",
            "pid",
            "agent",
            "last_activity_ts",
            "session_project_id",
            "started_at",
            "ended_at",
            "exit_code",
            "external_session_id",
            "unread_count",
            "current_tool",
        },
    ),
]

REQUEST_CONTRACTS = [
    (
        "create-session",
        _fields(CreateSessionBody),
        # NewSessionModal sends cwd + agent (+ optional initial_prompt)
        {"cwd", "agent", "initial_prompt", "argv", "type", "session_project_id"},
    ),
    (
        "session-message",
        _fields(_SessionMessageBody),
        # sessions.ts sendMessage() body → chat stdin write
        {"text"},
    ),
    (
        "preferences-patch",
        _fields(PreferencePatch),
        # Settings.vue toggles + preferences.ts update()
        {
            "default_agent",
            "default_session_prompt_enabled",
            "default_session_prompt_note_enabled",
        },
    ),
    (
        "lark-patch",
        _fields(LarkSettingsPatch),
        # Settings.vue Lark enable switch → larkSettingsApi.update({ enabled })
        {"enabled"},
    ),
    (
        "lark-view",
        _fields(LarkSettingsView),
        # preferences.ts LarkSettings (response) — updated_at is optional/extra
        {
            "enabled",
            "chat_id",
            "user_id",
            "dedup_window_sec",
            "dnd_hours",
            "tz",
            "enabled_types",
            "cli_installed",
        },
    ),
]

# Appended rather than inlined so their absence costs exactly their own rows.
if PortRegistration is not None:
    ORM_CONTRACTS.append((
        "ports",
        _columns(PortRegistration),
        # mobile/frontend/src/api/ports.ts PortRow
        {
            "port",
            "name",
            "description",
            "pid",
            "process_cmd",
            "process_cwd",
            "owner_session_id",
            "status",
            "registered_at",
            "last_verified_at",
        },
    ))
if Feedback is not None:
    ORM_CONTRACTS.append((
        "feedback",
        _columns(Feedback),
        # mobile/frontend/src/api/feedback.ts FeedbackItem
        {"id", "category", "content", "page_path", "status", "created_at",
         "resolved_at"},
    ))
    REQUEST_CONTRACTS.append((
        "feedback-create",
        _fields(FeedbackCreateRequest),
        # feedback.ts submit() body
        {"category", "content", "page_path"},
    ))


@pytest.mark.parametrize("label, actual, expected", ORM_CONTRACTS, ids=lambda v: v if isinstance(v, str) else "")
def test_response_fields_exist_as_columns(label, actual, expected):
    missing = expected - actual
    assert not missing, (
        f"[{label}] mobile client reads fields the backend no longer exposes as "
        f"columns: {sorted(missing)}. Backend actual columns: {sorted(actual)}"
    )


@pytest.mark.parametrize("label, actual, expected", REQUEST_CONTRACTS, ids=lambda v: v if isinstance(v, str) else "")
def test_request_fields_are_accepted(label, actual, expected):
    missing = expected - actual
    assert not missing, (
        f"[{label}] mobile client sends fields the backend model no longer "
        f"accepts: {sorted(missing)}. Backend actual fields: {sorted(actual)}"
    )
