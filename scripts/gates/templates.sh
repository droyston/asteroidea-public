#!/bin/sh
# The synthesized CDK templates of this checkout equal those of BASE, apart from asset hashes.
# Usage: BASE=<ref> sh scripts/gates/templates.sh   (make gate-templates BASE=origin/main)
# Steps: resolve BASE to a SHA; add a detached base worktree at .tools/template-gate/base; uv sync there;
# synth '*' in base and head under two contexts (plain, and sandbox-test as deploys and synth tests use);
# compare with asteroidea_infra.template_compare (head's copy); remove the base worktree.
# Exit 0: "templates equal: 4 files x 2 contexts". Exit 1: DIFF lines (first 50). Exit 2: cannot run.
# Expected diff: when infra/template-diff.expected differs from BASE's copy (absent counts as empty), the
# gate exits 0 only if the DIFF lines equal that file's non-comment lines ("templates differ as expected:
# <n> lines"); otherwise exit 1 with UNEXPECTED / MISSING lines (first 50 each).
# The base worktree is removed on every exit path after it is made, never with --force.
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root" || exit 2
unset VIRTUAL_ENV
cdk_version=2.1146.1
base_ref=${BASE:-origin/main}

sha=$(git rev-parse --verify --quiet "${base_ref}^{commit}") || { echo "cannot resolve $base_ref"; exit 2; }
work=$root/.tools/template-gate
base_dir=$work/base
out=$work/out
if [ -e "$base_dir" ]; then
  echo "$base_dir exists: a previous run did not clean up. Run: git worktree remove $base_dir"
  exit 2
fi
mkdir -p "$work" || exit 2

made=
cleanup() {
  st=$?
  rm -rf "$out"
  if [ -n "$made" ]; then
    if ! git worktree remove "$base_dir"; then
      echo "could not remove $base_dir; run: git worktree remove $base_dir"
      st=2
    fi
  fi
  exit "$st"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

git worktree add --detach "$base_dir" "$sha" >/dev/null 2>&1 || { echo "cannot add worktree $base_dir at $sha"; exit 2; }
made=1
rm -rf "$out"
echo "base $base_ref = $sha"

(cd "$base_dir" && uv sync --all-packages --frozen >/dev/null 2>&1) || { echo "uv sync failed in $base_dir"; exit 2; }

synth() { # synth <checkout> <side> <context> <cdk -c args...>
  checkout=$1 side=$2 context=$3
  shift 3
  (cd "$checkout" && uv run --frozen npx --yes "aws-cdk@$cdk_version" synth '*' -o "$out/$side/$context" --quiet "$@" >"$out/$side-$context.log" 2>&1) \
    || { echo "synth failed: $side $context"; tail -20 "$out/$side-$context.log"; return 1; }
}
mkdir -p "$out" || exit 2
for side in base head; do
  if [ "$side" = base ]; then checkout=$base_dir; else checkout=$root; fi
  synth "$checkout" "$side" plain -c raw-retention-days=1 || exit 2
  synth "$checkout" "$side" sandbox-test -c sandbox-test=true -c raw-retention-days=1 || exit 2
done

expected_file=infra/template-diff.expected
if git cat-file -e "$sha:$expected_file" 2>/dev/null; then
  git show "$sha:$expected_file" >"$out/expected.base" || exit 2
else
  : >"$out/expected.base"
fi
if [ -f "$root/$expected_file" ]; then cp "$root/$expected_file" "$out/expected.head"; else : >"$out/expected.head"; fi
if cmp -s "$out/expected.base" "$out/expected.head"; then
  uv run --frozen python -m asteroidea_infra.template_compare "$out/base" "$out/head"
else
  uv run --frozen python -m asteroidea_infra.template_compare "$out/base" "$out/head" --expected "$out/expected.head"
fi
