#!/usr/bin/env bash
# Alerts via ntfy.sh when podman's reclaimable (dangling) image storage crosses
# a threshold. Runs directly on the HOST as root (not via podman exec) — the
# root image store (/var/lib/containers/storage, used by systemd / sudo podman
# build during hotfixes) isn't reachable from inside the app container.
# See docs/cron-indexing.md for the cron setup.
#
# Usage: NTFY_TOPIC_URL=https://ntfy.sh/your-topic IMAGE_BLOAT_THRESHOLD_GB=10 \
#        ./bin/check_image_bloat.sh

set -euo pipefail

THRESHOLD_GB="${IMAGE_BLOAT_THRESHOLD_GB:-10}"
STATE_FILE="${IMAGE_BLOAT_STATE_FILE:-/var/lib/zotero-rag-image-bloat-state}"

reclaimable_line=$(podman system df 2>/dev/null | awk '/^Images/ {print $0}')
# Example line: "Images         235         6           17.54GB     15.61GB (89%)"
reclaimable_gb=$(echo "$reclaimable_line" | awk '{print $5}' | sed -E 's/GB.*//; s/MB.*/0/; s/kB.*/0/; s/B.*/0/')

is_unhealthy=0
if awk -v a="$reclaimable_gb" -v b="$THRESHOLD_GB" 'BEGIN{exit !(a>=b)}'; then
  is_unhealthy=1
fi

was_unhealthy=0
[ -f "$STATE_FILE" ] && was_unhealthy=$(cat "$STATE_FILE")

if [ -n "${NTFY_TOPIC_URL:-}" ]; then
  if [ "$is_unhealthy" = "1" ] && [ "$was_unhealthy" != "1" ]; then
    curl -fsS -H "Title: zotero-rag: podman image bloat" -H "Priority: default" \
      -d "Reclaimable image storage: ${reclaimable_gb}GB (threshold: ${THRESHOLD_GB}GB). Run: sudo podman image prune -f" \
      "$NTFY_TOPIC_URL" >/dev/null || true
  elif [ "$is_unhealthy" = "0" ] && [ "$was_unhealthy" = "1" ]; then
    curl -fsS -H "Title: zotero-rag: podman image bloat cleared" -H "Priority: default" \
      -d "Reclaimable image storage back under ${THRESHOLD_GB}GB." \
      "$NTFY_TOPIC_URL" >/dev/null || true
  fi
fi

echo "$is_unhealthy" > "$STATE_FILE"

if [ "$is_unhealthy" = "1" ]; then
  echo "UNHEALTHY: reclaimable image storage is ${reclaimable_gb}GB (threshold ${THRESHOLD_GB}GB)" >&2
  exit 1
fi
echo "OK: reclaimable image storage is ${reclaimable_gb}GB"
