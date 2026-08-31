#!/usr/bin/env bash
# lark_notify.sh <target_kind> <target_id> <message_file>
#
# target_kind : "chat" | "user" | "bot"
# target_id   : chat_id / open_id / bot chat_id
# message_file: path to a text or markdown file whose contents to send
#
# Default behaviour: write the message to $CSM_MISSION_WS/lark_outbox/<ts>.md
# instead of actually sending. Set CSM_LARK_MODE=real to actually invoke
# lark-cli. Idempotency key derived from CSM_MISSION_ID + target_id + msg hash.
#
# On completion prints:
#   {"sent": bool, "target_kind": "...", "target_id": "...", "sink": "..."}

set -euo pipefail

kind="${1:?target_kind required}"
target="${2:?target_id required}"
msg_file="${3:?message_file required}"

mode="${CSM_LARK_MODE:-mock}"
ws="${CSM_MISSION_WS:-/tmp}"
mission_id="${CSM_MISSION_ID:-manual-$$}"

if [ ! -f "$msg_file" ]; then
    echo "lark_notify: message file $msg_file not found" >&2
    exit 2
fi

# Idempotency key stable per (mission, target, content).
msg_hash=$(sha256sum "$msg_file" | awk '{print substr($1,1,12)}')
idem="csm-${mission_id}-${target}-${msg_hash}"

if [ "$mode" != "real" ]; then
    outbox="$ws/lark_outbox"
    mkdir -p "$outbox"
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    out="$outbox/${ts}_${kind}_${target}.md"
    {
        printf '# [MOCK Lark send]\n'
        printf 'kind: %s\n' "$kind"
        printf 'target: %s\n' "$target"
        printf 'idempotency_key: %s\n' "$idem"
        printf 'ts: %s\n\n' "$ts"
        printf -- '---\n\n'
        cat "$msg_file"
    } > "$out"
    printf '{"sent":false,"target_kind":"%s","target_id":"%s","sink":"%s"}\n' \
        "$kind" "$target" "$out"
    exit 0
fi

# CSM_LARK_MODE=real — actually invoke lark-cli.
case "$kind" in
    chat|bot)
        lark-cli im +messages-send --as bot --chat-id "$target" \
            --idempotency-key "$idem" --content "@$msg_file" >&2
        ;;
    user)
        lark-cli im +messages-send --as user --open-id "$target" \
            --idempotency-key "$idem" --content "@$msg_file" >&2
        ;;
    *)
        echo "lark_notify: unknown kind $kind" >&2
        exit 3
        ;;
esac

printf '{"sent":true,"target_kind":"%s","target_id":"%s","sink":"lark-cli"}\n' \
    "$kind" "$target"
