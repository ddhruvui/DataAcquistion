#!/usr/bin/env bash
# Download volume files into the repo root, mirroring keys (so data/ohlcv/GOOG.json
# -> ./data/ohlcv/GOOG.json). With no argument, mirrors every file EXCEPT code/.
#
#   scripts/download.sh                            whole volume (the daily mirror)
#   scripts/download.sh data/calendar/US.json      one file -> ./data/calendar/US.json
#   scripts/download.sh data/market/ data/ohlcv/   every file under those prefixes
#
# A trailing / makes an argument a prefix; anything else is taken as an exact key.
# Each object is fetched with `s3api get-object` (a pure GetObject, no HeadObject —
# which RunPod 403s on freshly pod-written files).
. "$(dirname "$0")/_common.sh"

REPO_ROOT="$ROOT"

# List keys under a prefix ("" = the whole volume). The first list of freshly
# pod-written files can be slow or return a duplicate next-token; retry a few times
# before giving up.
list_keys() {
  for i in 1 2 3 4 5; do
    if KEYS=$(aws s3 ls $S3FLAGS "$BUCKET/${1:-}" --recursive 2>/dev/null | awk '{$1=$2=$3=""; sub(/^ +/,""); print}'); then
      [ -n "$KEYS" ] && { printf '%s\n' "$KEYS"; return 0; }
    fi
    sleep 3
  done
  return 1
}

# Fetch one key to its mirrored path under the repo root.
fetch_key() {
  key="$1"; dest="$REPO_ROOT/$key"
  mkdir -p "$(dirname "$dest")"
  echo "  $key"
  for i in 1 2 3 4 5; do
    # write to a temp file and rename only on success, so a failed/partial fetch
    # never leaves a truncated file behind.
    if aws s3api get-object $S3FLAGS --bucket "$RUNPOD_VOLUME_ID" --key "$key" "$dest.part" >/dev/null 2>&1; then
      mv "$dest.part" "$dest"; return 0
    fi
    rm -f "$dest.part"; sleep 3
  done
  echo "FAILED to download $key" >&2
  return 1
}

n=0

if [ "$#" -gt 0 ]; then
  for arg in "$@"; do
    arg="${arg#s3://$RUNPOD_VOLUME_ID/}"   # tolerate a pasted s3:// URI
    arg="${arg#/}"                         # ...and a leading slash
    [ -n "$arg" ] || continue
    case "$arg" in
      */)  # prefix: download everything under it
        KEYS=$(list_keys "$arg") || { echo "nothing under $arg on $BUCKET" >&2; exit 1; }
        while IFS= read -r key; do
          [ -n "$key" ] || continue
          case "$key" in */) continue ;; esac   # skip S3 directory-marker keys
          fetch_key "$key" || exit 1
          n=$((n + 1))
        done <<< "$KEYS"
        ;;
      *) fetch_key "$arg" || exit 1; n=$((n + 1)) ;;
    esac
  done
  # Name the actual destination — files land at their MIRRORED key path, not the repo
  # root itself, and most of those paths are gitignored (so editor search hides them).
  if [ "$n" = 1 ]; then
    echo "Downloaded 1 file: $dest"
  else
    echo "Downloaded $n file(s), mirrored under $REPO_ROOT/ (last: $dest)"
  fi
  exit 0
fi

KEYS=$(list_keys) || { echo "nothing on $BUCKET (run scripts/launch.sh first)" >&2; exit 1; }

while IFS= read -r key; do
  [ -n "$key" ] || continue
  case "$key" in
    code/*) continue ;;   # skip uploaded code
    */) continue ;;       # skip S3 directory-marker keys
  esac
  fetch_key "$key" || exit 1
  n=$((n + 1))
done <<< "$KEYS"

echo "Downloaded $n file(s) to $REPO_ROOT/ (data/ at repo root)"
