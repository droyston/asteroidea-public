#!/bin/sh
# Run `make doctor` under four stub node versions; assert only the node line.
# The exit status of make doctor is not asserted: any stub node also breaks npx.
# GATE_MAKEFILE overrides the Makefile (used for red proofs).
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root" || exit 2
mk=${GATE_MAKEFILE:-Makefile}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/gate-doctor.XXXXXX") || exit 2
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
fail=0
check() { # version expected-line
  mkdir -p "$tmp/$1/bin"
  printf '#!/bin/sh\necho %s\n' "$1" > "$tmp/$1/bin/node"
  chmod +x "$tmp/$1/bin/node"
  PATH="$tmp/$1/bin:$PATH" make -f "$mk" doctor > "$tmp/$1/out" 2>&1
  got=$(grep -E '^(ok|FAIL) +node ' "$tmp/$1/out")
  if [ "$got" = "$2" ]; then echo "pass  $got"
  else echo "MISMATCH for $1: expected '$2', got '$got'"; fail=1; fi
}
check v22.11.0 "FAIL  node v22.11.0 (need >=22.12, not 25)"
check v22.12.0 "ok    node v22.12.0"
check v25.1.0 "FAIL  node v25.1.0 (need >=22.12, not 25)"
check v26.8.2 "ok    node v26.8.2"
if [ "$fail" -eq 0 ]; then echo "gate-doctor: holds"; fi
exit "$fail"
