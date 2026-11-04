#!/bin/sh
# Live gates (network, not in make check): the harvest count check and the peek byte bound.
# Logs come from the run this script starts. Env: GATE_OUT (log dir, default a temp dir),
# PEEK_MAX_BYTES (default 14105034, the asset contentSize; lower it for a red proof).
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root" || exit 2
out=${GATE_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/gate-live.XXXXXX")}
mkdir -p "$out"
max=${PEEK_MAX_BYTES:-14105034}
fail=0
echo "gate-live: logs in $out"

# Harvest count check: harvest, then compare listed counts with the live API counts from the same run.
uv run --frozen python -m asteroidea_services.harvest --out "$out/index.json" 2> "$out/harvest.log"
rc=$?
echo "harvest exit $rc"
[ "$rc" -eq 0 ] || { echo "harvest-count FAIL: harvest exit $rc (see $out/harvest.log)"; tail -3 "$out/harvest.log"; fail=1; }
if [ "$rc" -eq 0 ]; then
  dandi_api=$(curl -fsS "https://api.dandiarchive.org/api/dandisets/?page_size=1&draft=false&empty=false" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])')
  open_api=$(curl -fsS -X POST https://openneuro.org/crn/graphql -H 'Content-Type: application/json' \
    -d '{"query":"{datasets(first:1){pageInfo{count}}}"}' |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["datasets"]["pageInfo"]["count"])')
  for src in dandi openneuro; do
    listed=$(sed -n "s/^$src: listed \([0-9]*\), ok .*/\1/p" "$out/harvest.log" | tail -1)
    failed=$(sed -n "s/^$src: listed .*failed \([0-9]*\),.*/\1/p" "$out/harvest.log" | tail -1)
    api=$dandi_api; [ "$src" = openneuro ] && api=$open_api
    echo "$src listed $listed, api count $api, failed $failed"
    if [ -z "$listed" ] || [ "$listed" != "$api" ]; then
      echo "harvest-count FAIL: $src listed ${listed:-none} != api count $api"; fail=1
    fi
    if [ -n "$listed" ] && [ "$((failed * 100))" -gt "$listed" ]; then
      echo "harvest-count FAIL: $src failed $failed of $listed exceeds 1%"; fail=1
    fi
  done
fi

# Peek byte bound: peek one DANDI asset; units, lfp and electricalseries present; bytes below bound.
uv run --frozen python -m asteroidea_services.peek --dandiset 000576 --version 0.231010.1811 \
  --asset 20257c14-af6a-4f3a-86c4-0b9bfc120714 > "$out/peek.log" 2>&1
rc=$?
echo "peek exit $rc"
[ "$rc" -eq 0 ] || { echo "peek FAIL: peek exit $rc"; tail -3 "$out/peek.log"; fail=1; }
grep -q '^units: present' "$out/peek.log" || { echo "peek FAIL: no 'units: present' line"; fail=1; }
grep -q '^lfp: ' "$out/peek.log" || { echo "peek FAIL: no lfp: line"; fail=1; }
grep -q '^electricalseries: ' "$out/peek.log" || { echo "peek FAIL: no electricalseries: line"; fail=1; }
bytes=$(sed -n 's/^read \([0-9]*\) of [0-9]* bytes.*/\1/p' "$out/peek.log")
echo "peek bytes read ${bytes:-none}, bound $max"
if [ -z "$bytes" ] || [ "$bytes" -ge "$max" ]; then
  echo "peek FAIL: bytes read ${bytes:-none} not below $max"; fail=1
fi

if [ "$fail" -eq 0 ]; then echo "gate-live: harvest counts and peek bound hold"; fi
echo "EXIT=$fail"
exit "$fail"
