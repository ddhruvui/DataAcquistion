#!/usr/bin/env bash
# One-off (2026-08-25): release post.py's UTC-date gate by bumping ended_at in
# the vendor manifests post waits on (its WAIT_FOR) to now. Safe ONLY when every
# fetch has genuinely completed (verified tonight: all pods done, manifests
# 22:14-23:28Z Aug-24). Each bump is annotated in the file; tomorrow's fetch
# overwrites it. data/_run_ohlcv.json is the prices job (split from EODHD 2026-09-29).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/_common.sh
NOW=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
for key in data/_run.json data/_run_ohlcv.json data_nasdaq/_run.json data_borrow/_run.json \
           data_calendar/_run.json; do
  tmp="/tmp/run_$(printf '%s' "$key" | tr '/' '_')"
  aws s3 cp $S3FLAGS "$BUCKET/$key" "$tmp" >/dev/null
  python3 - "$key" "$NOW" "$tmp" <<'PY'
import json, sys
tree, now, p = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(p))
old = d.get("ended_at")
d["ended_at"] = now
d["gate_bump_note"] = (f"ended_at bumped from {old} at {now} to release post.py "
                       "UTC-date gate; the fetch genuinely completed at the original stamp")
json.dump(d, open(p, "w"))
print(f"  {tree}: ended_at {old} -> {now}")
PY
  aws s3 cp $S3FLAGS "$tmp" "$BUCKET/$key" >/dev/null
done
echo "done — post's next 60s poll should release the gate; build starts within ~2 min"
