#!/bin/sh
# rf-images SHA-pinned: the compute and extract image rows of `make rf-budget`. Offline.
# An image counts only when it carries the tag <repository>:<git rev-parse HEAD>, which
# `make compute-image` and `make extract-image` add after a build of a clean tree. An image
# tagged for another commit (or only the moving `:local` tag) is not used: the row prints
# not-measured and fails, unless RF_BUDGET_OFFLINE=1, which marks it and passes.
# Prints the two table rows (same columns as rf-budget.sh); exit 1 on a STOP or an unmeasured row.
# Env: IMAGE_MAX_BYTES (default 1200000000), COMPUTE_IMAGE / EXTRACT_IMAGE (repository names,
#      default asteroidea-compute / asteroidea-extract), RF_BUDGET_OFFLINE=1.
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root" || exit 2
fail=0
max_bytes=${IMAGE_MAX_BYTES:-1200000000}
compute_repo=${COMPUTE_IMAGE:-asteroidea-compute}
extract_repo=${EXTRACT_IMAGE:-asteroidea-extract}
extract_baseline=1061799673
extract_tol=1048576
sha=$(git rev-parse HEAD) || exit 2

bad() { echo "STOP: $*"; fail=1; }
image_size() { docker image inspect -f '{{.Size}}' "$1" 2>/dev/null; }
row() { printf '%-18s %12s %12s %10s %10s  %s\n' "$@"; }

# unmeasured <label> <baseline> <estimate> <make target>
unmeasured() {
  if [ "${RF_BUDGET_OFFLINE:-0}" = 1 ]; then
    row "$1" "$2" not-measured - "$3" "not-measured (RF_BUDGET_OFFLINE=1: passes, marked)"
  else
    row "$1" "$2" not-measured - "$3" "-"
    bad "not-measured: no docker image ${4}:${sha} (an image tagged for another commit is not used); run make ${5} at this HEAD with a clean tree, or set RF_BUDGET_OFFLINE=1"
  fi
}

csize=$(image_size "${compute_repo}:${sha}")
if [ -z "$csize" ]; then
  unmeasured "compute image (B)" none "<=$max_bytes" "$compute_repo" compute-image
else
  v=ok
  if [ "$csize" -gt "$max_bytes" ]; then v="STOP (over IMAGE_MAX_BYTES)"; fail=1; fi
  row "compute image (B)" none "$csize" "-" "<=$max_bytes" "$v"
fi

esize=$(image_size "${extract_repo}:${sha}")
if [ -z "$esize" ]; then
  unmeasured "extract image (B)" "$extract_baseline" "<=$extract_tol" "$extract_repo" extract-image
else
  ed=$((esize - extract_baseline)); v=ok
  if [ "$ed" -gt "$extract_tol" ]; then v="STOP (growth over 1 MB)"; fail=1; fi
  row "extract image (B)" "$extract_baseline" "$esize" "$ed" "<=$extract_tol" "$v"
fi

exit "$fail"
