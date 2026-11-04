#!/bin/sh
# rf-budget: the cumulative resource budget (bundle, images, rates latency). Offline.
# Per resource: current size against its cap; exit 1 when a hard cap is crossed.
# The image rows are scripts/gates/rf-images.sh: an image counts only when tagged with the
# current `git rev-parse HEAD` (make compute-image / extract-image add that tag); none is
# not-measured and fails.
# Env: RF_BUDGET_LIVE_DIR (default ~/asteroidea-live),
#      RF_BUDGET_OFFLINE=1 (no latency file or no image for HEAD: print not-measured and pass,
#      marked), RF_BUDGET_SKIP_BUILD=1 (reuse web/dist), IMAGE_MAX_BYTES (default
#      1200000000), COMPUTE_IMAGE / EXTRACT_IMAGE (docker repository names).
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root" || exit 2
fail=0
live_dir=${RF_BUDGET_LIVE_DIR:-$HOME/asteroidea-live}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/rf-budget.XXXXXX") || exit 2
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

bad() { echo "STOP: $*"; fail=1; }
num() { node -e 'const b=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));const v=b[process.argv[2]];process.stdout.write(String(v))' "$1" "$2"; }

printf '%-18s %12s %12s %10s %10s  %s\n' resource baseline current delta cap verdict

# --- Main JS and research chunks (fresh vite build, web/scripts/bundle-size.mjs) ---
budget=web/bundle-budget.json
cap_kb=$(num "$budget" cap_kb)
if [ "${RF_BUDGET_SKIP_BUILD:-0}" != 1 ]; then
  if ! (cd web && ./node_modules/.bin/vite build) > "$tmp/vite.log" 2>&1; then
    cat "$tmp/vite.log"; bad "vite build failed"
  fi
fi
(cd web && node scripts/bundle-size.mjs) > "$tmp/bundle.out" 2> "$tmp/bundle.err"
bundle_rc=$?
cur_kb=$(sed -n 's/^bundle: main .* \([0-9.]*\) KB gz, cap.*/\1/p' "$tmp/bundle.out")
if [ -z "$cur_kb" ]; then
  cat "$tmp/bundle.err"; bad "no main size from bundle-size.mjs"; cur_kb=0
fi
verdict=ok
if node -e 'process.exit(Number(process.argv[1])>Number(process.argv[2])?0:1)' "$cur_kb" "$cap_kb"; then
  verdict="STOP (over cap ${cap_kb} KB)"; fail=1
fi
printf '%-18s %12s %12s %10s %10s  %s\n' "main JS (KB gz)" none "$cur_kb" - "$cap_kb" "$verdict"

nchunks=$(grep -c '^bundle: lazy ' "$tmp/bundle.out")
printf '%-18s %12s %12s %10s %10s  %s\n' "research chunks" 0 "$nchunks" "$nchunks" "-" "chunk caps from the budget file via bundle-size.mjs"
if [ "$bundle_rc" -ne 0 ]; then cat "$tmp/bundle.err"; bad "bundle-size.mjs exited $bundle_rc"; fi

# --- Images ---
if ! sh scripts/gates/rf-images.sh; then fail=1; fi

# --- Rates latency (file for the current HEAD only) ---
sha=$(git rev-parse HEAD)
lat="$live_dir/rf-compute-$sha.json"
if [ -f "$lat" ]; then
  p50=$(num "$lat" warm_p50_s); fsha=$(num "$lat" sha)
  if [ "$fsha" != "$sha" ]; then
    bad "latency file $lat is stamped $fsha, HEAD is $sha"
  else
    v=ok
    if node -e 'process.exit(Number(process.argv[1])>6?0:1)' "$p50"; then v="STOP (warm p50 over 6 s)"; fail=1; fi
    printf '%-18s %12s %12s %10s %10s  %s\n' "rates warm p50 (s)" none "$p50" - 4 "$v"
  fi
else
  other=$(ls "$live_dir"/rf-compute-*.json 2>/dev/null | head -1)
  if [ -n "$other" ]; then
    osha=$(num "$other" sha)
    bad "latency file $other is stamped $osha, HEAD is $sha (no file for HEAD)"
  elif [ "${RF_BUDGET_OFFLINE:-0}" = 1 ]; then
    printf '%-18s %12s %12s %10s %10s  %s\n' "rates warm p50 (s)" none not-measured - 4 "not-measured (RF_BUDGET_OFFLINE=1: passes, marked)"
  else
    printf '%-18s %12s %12s %10s %10s  %s\n' "rates warm p50 (s)" none not-measured - 4 "-"
    bad "not-measured: no $lat; run make live T=tests/live/test_rf_compute.py, or set RF_BUDGET_OFFLINE=1"
  fi
fi

if [ "$fail" -eq 0 ]; then echo "rf-budget: within budget"; else echo "rf-budget: FAIL"; fi
exit "$fail"
