#!/usr/bin/env bash
# tmux_socket_name.sh <base_socket_name>
#
# Compute a tmux socket / session name for the current mission. Prefixes with
# csm-<mission_id>- so parallel missions and production tmux sessions never
# collide.
#
# Emits the resolved name to stdout.

set -euo pipefail

base="${1:?base_socket_name required}"
mission_id="${CSM_MISSION_ID:-manual-$$}"

printf 'csm-%s-%s\n' "$mission_id" "$base"
