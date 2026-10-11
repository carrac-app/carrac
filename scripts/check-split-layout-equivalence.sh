#!/usr/bin/env bash
# Did my edit to a static/js file change the PROGRAM, or only its formatting?
#
# Minifies the working-tree copy of a file and the copy at a git ref with the
# deploy pipeline's exact terser flags, then compares the bytes. Identical
# output means the two sources compile to the same program — which is how
# de-minifying split-layout.js was shown to change nothing.
#
# This is a TOOL, not an invariant: a real behaviour change is SUPPOSED to make
# it differ. Reach for it when you expect "no functional change" and want that
# checked rather than asserted — refactors, reformatting, de-minification.
#
#   scripts/check-split-layout-equivalence.sh                      # split-layout.js vs HEAD
#   scripts/check-split-layout-equivalence.sh static/js/foo.js HEAD~3
#
# The permanent guard against a file going minified again is a different thing
# and lives in tests/test_no_minified_sources.py.
# -h / --help: print the header comment above and stop, before anything runs.
case "${1:-}" in
    -h|--help)
        awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} /^$/{print; next} {exit}' "$0"
        exit 0 ;;
esac

set -uo pipefail

FILE="${1:-static/js/split-layout.js}"
REF="${2:-HEAD}"

cd "$(git rev-parse --show-toplevel)" || exit 1

if [ ! -f "$FILE" ]; then
    printf '\033[31m✗ no such file: %s\033[0m\n' "$FILE"
    exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if ! git show "$REF:$FILE" > "$TMP/ref-src.js" 2>/dev/null; then
    printf '\033[31m✗ %s does not exist at %s\033[0m\n' "$FILE" "$REF"
    exit 1
fi

# Same flags as Makefile `minify`.
TERSER_ARGS=(--compress "drop_console=true" --mangle)

if ! npx terser "$TMP/ref-src.js" "${TERSER_ARGS[@]}" -o "$TMP/ref.min.js" 2>"$TMP/err-ref"; then
    printf '\033[31m✗ terser failed on %s at %s\033[0m\n' "$FILE" "$REF"; cat "$TMP/err-ref"; exit 1
fi
if ! npx terser "$FILE" "${TERSER_ARGS[@]}" -o "$TMP/new.min.js" 2>"$TMP/err-new"; then
    printf '\033[31m✗ terser failed on the working-tree %s\033[0m\n' "$FILE"; cat "$TMP/err-new"; exit 1
fi

REF_BYTES=$(wc -c < "$TMP/ref.min.js")
NEW_BYTES=$(wc -c < "$TMP/new.min.js")

if cmp -s "$TMP/ref.min.js" "$TMP/new.min.js"; then
    printf '\033[32m✓ %s compiles to the same program as at %s\033[0m (%s bytes minified)\n' \
        "$FILE" "$REF" "$REF_BYTES"
    exit 0
fi

printf '\033[33m⚠ %s compiles DIFFERENTLY from %s\033[0m\n' "$FILE" "$REF"
printf '  minified: %s bytes at %s, %s bytes in the working tree\n' "$REF_BYTES" "$REF" "$NEW_BYTES"
FIRST=$(cmp "$TMP/ref.min.js" "$TMP/new.min.js" 2>/dev/null | grep -oE 'byte [0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -n "${FIRST:-}" ]; then
    START=$(( FIRST > 70 ? FIRST - 70 : 1 ))
    printf '  first difference at byte %s:\n' "$FIRST"
    printf '    \033[36m%s\033[0m: %s\n' "$REF" "$(cut -c${START}-$((FIRST + 90)) "$TMP/ref.min.js")"
    printf '    \033[36mworking\033[0m: %s\n' "$(cut -c${START}-$((FIRST + 90)) "$TMP/new.min.js")"
fi
printf '\n  If you intended a behaviour change, this is the expected result.\n'
printf '  If you intended none, the diff above is where it crept in.\n'
exit 2
