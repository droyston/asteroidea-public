#!/bin/sh
# Fail on any lint or type-checker suppression comment that
# the allowlist does not list. One gate for the whole repo.
# Families: `# noqa` and `# ruff: noqa`; `# ty: ignore`; `# type: ignore`; `biome-ignore`
# (also -all, -start, -end); `@ts-ignore`; `@ts-expect-error`; `@ts-nocheck`. Scanned with
# `git grep` over tracked and untracked-not-ignored *.py, *.ts, *.tsx, *.js, *.jsx, *.mjs and
# *.cjs files. Each hit reduces to a `path:KEY` key, with multiplicity:
#   noqa            -> CODE (ruff code; a bare `# noqa` is `*`)
#   ty: ignore      -> ty-ignore[RULE]         (no bracket: ty-ignore[*])
#   type: ignore    -> type-ignore[RULE]       (no bracket: type-ignore[*])
#   biome-ignore    -> biome-ignore[lint/..]   (the rule after the keyword; none: biome-ignore[*])
#   @ts-ignore etc. -> ts-ignore, ts-expect-error, ts-nocheck
# scripts/suppression-allowlist.txt holds `path:KEY: reason`, one line per occurrence; blank
# lines and `#` lines are ignored.
# Near miss: prose or a docstring that holds the bare word "noqa" is not a hit, because the
# family needs its comment marker (`#` or `//`) before `noqa`, `ty: ignore` or `type: ignore`.
# A docstring that holds the literal text "# noqa" is a hit; reword it or allowlist it.
# Exit 0 when every hit is allowlisted and no allowlist line is stale; exit 1 and print
# SUPPRESSION / STALE / BAD lines otherwise. GATE_ROOT and SUPPRESSION_ALLOWLIST override the
# tree and the allowlist (used for red proofs).
root=${GATE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
cd "$root" || exit 2
allow=${SUPPRESSION_ALLOWLIST:-scripts/suppression-allowlist.txt}
[ -f "$allow" ] || { echo "suppression-gate: missing $allow"; exit 2; }
hits=$(mktemp) || exit 2
trap 'rm -f "$hits"' EXIT

exclude=
git grep -n -I -i -E --untracked \
  '(#[[:space:]]*(ruff:[[:space:]]*)?noqa|[#/][[:space:]]*(ty|type):[[:space:]]*ignore|biome-ignore|@ts-(ignore|expect-error|nocheck))' \
  -- '*.py' '*.ts' '*.tsx' '*.js' '*.jsx' '*.mjs' '*.cjs' $exclude >"$hits"
rc=$?
if [ "$rc" -gt 1 ]; then echo "suppression-gate: git grep failed (exit $rc)"; exit 2; fi

awk -v allow="$allow" '
function add(path, lineno, key) {
  key = path ":" key
  h[key]++; where[key] = where[key] " " lineno
}
function after(text, low, word,   i) {
  i = index(low, word)
  return i ? substr(text, i + length(word)) : ""
}
function codes(rest,   n, parts, k, tok, out) {
  sub(/^[ \t]*:[ \t]*/, "", rest)
  n = split(rest, parts, /[ \t,]+/)
  out = ""
  for (k = 1; k <= n; k++) {
    tok = parts[k]
    if (tok !~ /^[A-Za-z]+[0-9]+$/) break
    out = out (out == "" ? "" : " ") toupper(tok)
  }
  return out == "" ? "*" : out
}
function bracket(rest,   r) {
  if (rest !~ /^\[/) return "*"
  r = substr(rest, 2)
  sub(/\].*$/, "", r)
  gsub(/[ \t]+/, "", r)
  return r == "" ? "*" : r
}
function classify(path, lineno, text,   low, rest, n, cs, k, i, j) {
  low = tolower(text)
  if (low ~ /#[ \t]*(ruff:[ \t]*)?noqa/) {
    rest = after(text, low, "noqa")
    if (rest !~ /^[ \t]*:/) add(path, lineno, "*")
    else { n = split(codes(rest), cs, " "); for (k = 1; k <= n; k++) add(path, lineno, cs[k]) }
  }
  if (low ~ /[#\/][ \t]*ty:[ \t]*ignore/) {
    i = match(low, /ty:[ \t]*ignore/)
    rest = substr(text, i + RLENGTH)
    n = split(bracket(rest), cs, ","); for (k = 1; k <= n; k++) add(path, lineno, "ty-ignore[" cs[k] "]")
  }
  if (low ~ /[#\/][ \t]*type:[ \t]*ignore/) {
    i = match(low, /type:[ \t]*ignore/)
    rest = substr(text, i + RLENGTH)
    n = split(bracket(rest), cs, ","); for (k = 1; k <= n; k++) add(path, lineno, "type-ignore[" cs[k] "]")
  }
  if (low ~ /biome-ignore/) {
    i = match(low, /biome-ignore(-all|-start|-end)?/)
    rest = substr(text, i + RLENGTH)
    sub(/^[ \t]+/, "", rest)
    j = match(rest, /^[A-Za-z0-9\/_.-]+/)
    add(path, lineno, "biome-ignore[" (j ? substr(rest, 1, RLENGTH) : "*") "]")
  }
  if (low ~ /@ts-ignore/) add(path, lineno, "ts-ignore")
  if (low ~ /@ts-expect-error/) add(path, lineno, "ts-expect-error")
  if (low ~ /@ts-nocheck/) add(path, lineno, "ts-nocheck")
}
BEGIN {
  fail = 0
  while ((getline line < allow) > 0) {
    if (line ~ /^[ \t]*(#|$)/) continue
    i = index(line, ":"); j = index(substr(line, i + 1), ":")
    reason = j ? substr(line, i + j + 1) : ""
    gsub(/^[ \t]+|[ \t]+$/, "", reason)
    if (i == 0 || j == 0 || reason == "") {
      print "BAD allowlist line (need path:KEY: reason): " line; fail = 1; continue
    }
    a[substr(line, 1, i + j - 1)]++
  }
}
{
  i = index($0, ":"); path = substr($0, 1, i - 1)
  rest = substr($0, i + 1); j = index(rest, ":")
  classify(path, substr(rest, 1, j - 1), substr(rest, j + 1))
}
END {
  for (key in h) {
    if (h[key] > a[key] + 0) {
      printf "SUPPRESSION not allowlisted: %s (%d found, %d allowed; line%s)\n", key, h[key], a[key] + 0, where[key]
      fail = 1
    }
  }
  for (key in a) {
    if (a[key] > h[key] + 0) {
      printf "STALE allowlist entry: %s (%d listed, %d found)\n", key, a[key], h[key] + 0
      fail = 1
    }
  }
  if (!fail) {
    total = 0; for (key in h) total += h[key]
    printf "suppression-gate: %d suppression(s), every one allowlisted\n", total
  }
  exit fail
}' "$hits"
