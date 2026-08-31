"""Worktime tracking subsystem.

Two independent producers write append-only rows to `work_interval`:

- `WorktimeTracker` subscribes to EventStream for `MESSAGE_USER_SENT` and
  `MESSAGE_ASSISTANT_DONE`, running a per-session state machine to open and
  close `kind=agent` intervals. Session terminal events close any still-open
  interval; a 30-min safety cap prevents runaway rows from lost close events.

- `HeartbeatManager` accepts POST /api/worktime/heartbeat from the frontend
  and runs a per-user state machine for a single `kind=human` interval. A
  60s grace window bridges brief tab-inactivity; a sweeper task closes the
  interval when heartbeats lapse.

`WorktimeService` supplies the read-side aggregations (today totals + live
open counters) that back the top-right header widget, and the boot-time
reap that closes any interval left dangling by an unclean shutdown.
"""

from csm.modules.worktime.heartbeat import HeartbeatManager
from csm.modules.worktime.service import WorktimeService
from csm.modules.worktime.tracker import WorktimeTracker

__all__ = ["HeartbeatManager", "WorktimeService", "WorktimeTracker"]
