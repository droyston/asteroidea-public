#!/bin/sh
# Docs gate: stale scope phrases stay out.
# Offline. GATE_ROOT overrides the tree checked (used for red proofs on scratch copies).
# Exit 0 when all hold; exit 1 and print MISSING/STUB/STALE lines otherwise.
root=${GATE_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$root" || exit 2
fail=0


# The stale scope phrases must not return.
if grep -n -i "integration with DANDI or OpenNeuro\|reference models only\|integrate with it\|not integrated" \
  docs/src/03-context-and-scope.md; then
  echo "STALE phrases in docs/src/03-context-and-scope.md"; fail=1
fi


if [ "$fail" -eq 0 ]; then echo "gate-docs: ${held:-the stale-phrase check holds}"; fi
exit "$fail"
