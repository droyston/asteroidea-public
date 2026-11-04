#!/bin/sh
# Gate: `make deploy` refuses (recipe exit 3, which make reports as 2) before any cdk or deploy-check call when the docker daemon is down.
# Stubs docker and uv on PATH; the sandbox env is set so the sandbox guard passes. Offline, no AWS.
# GATE_MAKEFILE overrides the Makefile (used for red proofs).
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root" || exit 2
mk=${GATE_MAKEFILE:-Makefile}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/gate-deploy-docker.XXXXXX") || exit 2
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
fail=0
run() { # name docker-exit expected-make-exit expect-uv-called(0|1)
  mkdir -p "$tmp/$1/bin"
  printf '#!/bin/sh\nexit %s\n' "$2" > "$tmp/$1/bin/docker"
  printf '#!/bin/sh\necho "$@" >> %s/uv.calls\nexit 0\n' "$tmp/$1" > "$tmp/$1/bin/uv"
  chmod +x "$tmp/$1/bin/docker" "$tmp/$1/bin/uv"
  PATH="$tmp/$1/bin:$PATH" ASTEROIDEA_PROFILE=stub ASTEROIDEA_ACCOUNT=000000000000 \
    SANDBOX_ENV=/nonexistent make -f "$mk" deploy > "$tmp/$1/out" 2>&1
  code=$?
  if [ -f "$tmp/$1/uv.calls" ]; then called=1; else called=0; fi
  if [ "$code" = "$3" ] && [ "$called" = "$4" ] && { [ "$3" = 0 ] || grep -q '\[deploy\] Error 3' "$tmp/$1/out"; }; then echo "pass  $1: make deploy exit $code, uv called=$called"
  else echo "MISMATCH $1: exit $code (want $3), uv called=$called (want $4)"; cat "$tmp/$1/out"; fail=1; fi
}
run daemon-down 1 2 0
run daemon-up 0 0 1
if [ "$fail" -eq 0 ]; then echo "gate-deploy-docker: holds"; fi
exit "$fail"
