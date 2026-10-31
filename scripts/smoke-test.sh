#!/usr/bin/env bash
# End-to-end verification of a built lptools bundle.
#
#   scripts/smoke-test.sh <bin-dir>
#
# It does not just check "--help": it builds a super.img with lpmake, lists it
# with lpdump, unpacks it with lpunpack, and asserts the extracted partitions are
# byte-identical to the inputs. A bundle that passes this is usable by
# imgpatchtools-rs; a bundle that only prints help is not.
set -euo pipefail

BIN_DIR=${1:?smoke-test.sh: expected one argument (path to the bin/ directory of the bundle)}
BIN_DIR=$(cd "$BIN_DIR" && pwd)

green() { printf "\033[32m%s\033[0m" "$1"; }
fail() { printf "  %s %s\n" "$(printf "\033[31mFAIL\033[0m")" "$*"; exit 1; }
pass() { printf "  %s %s\n" "$(green PASS)" "$*"; }

need() { [ -x "$BIN_DIR/$1" ] || fail "missing executable: $BIN_DIR/$1"; }

for t in lpmake lpdump lpunpack; do need "$t"; done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "smoke test: $BIN_DIR"

# --- 1. every binary starts and parses its own flags ------------------------
for t in lpmake lpdump lpunpack; do
  # lpmake returns EX_USAGE for --help; successful startup is shown by its text.
  help_output=$("$BIN_DIR/$t" --help 2>&1) || true
  case "$help_output" in
    *[Uu]sage:*) ;;
    *) help_output=$("$BIN_DIR/$t" -h 2>&1) || true ;;
  esac
  case "$help_output" in
    *[Uu]sage:*) pass "$t help" ;;
    *) printf '%s\n' "$help_output" >&2
       fail "$t cannot print help (wrong arch / missing loader?)" ;;
  esac
done

# --- 2. lpmake -> lpdump -> lpunpack round trip -----------------------------
head -c 1048576 /dev/urandom > "$tmp/system.img"
head -c 2097152 /dev/urandom > "$tmp/vendor.img"

"$BIN_DIR/lpmake" \
  --device-size 67108864 \
  --metadata-size 65536 \
  --metadata-slots 2 \
  --super-name super \
  --group main:67108864 \
  --partition system:readonly:1048576:main \
  --partition vendor:readonly:2097152:main \
  --image "system=$tmp/system.img" \
  --image "vendor=$tmp/vendor.img" \
  --output "$tmp/super.img" >/dev/null \
  || fail "lpmake could not create a super.img"

size=$(wc -c < "$tmp/super.img")
[ "$size" -eq 67108864 ] || fail "super.img is $size bytes, expected 67108864"
pass "lpmake created a ${size}-byte super.img"

"$BIN_DIR/lpdump" "$tmp/super.img" > "$tmp/dump.txt" 2>&1 \
  || fail "lpdump cannot read the image it just wrote"
grep -q "system" "$tmp/dump.txt" || fail "lpdump output has no system partition"
grep -q "vendor" "$tmp/dump.txt" || fail "lpdump output has no vendor partition"
pass "lpdump lists system + vendor"

mkdir -p "$tmp/out"
if ! "$BIN_DIR/lpunpack" --slot=0 "$tmp/super.img" "$tmp/out" >"$tmp/unpack.log" 2>&1; then
  cat "$tmp/unpack.log" >&2
  fail "lpunpack failed on a lpmake-produced image"
fi

[ -f "$tmp/out/system.img" ] || fail "lpunpack did not extract system.img"
[ -f "$tmp/out/vendor.img" ] || fail "lpunpack did not extract vendor.img"
cmp -s "$tmp/system.img" "$tmp/out/system.img" || fail "system.img is not byte-identical"
cmp -s "$tmp/vendor.img" "$tmp/out/vendor.img" || fail "vendor.img is not byte-identical"
pass "lpunpack extracted both partitions byte-exactly"

# --- 3. optional sparse helpers ---------------------------------------------
if [ -x "$BIN_DIR/img2simg" ] && [ -x "$BIN_DIR/simg2img" ]; then
  "$BIN_DIR/img2simg" "$tmp/system.img" "$tmp/system.simg" >/dev/null 2>&1 \
    || fail "img2simg failed"
  "$BIN_DIR/simg2img" "$tmp/system.simg" "$tmp/system.raw" >/dev/null 2>&1 \
    || fail "simg2img failed"
  cmp -s "$tmp/system.img" "$tmp/system.raw" || fail "sparse round-trip mismatch"
  pass "img2simg -> simg2img round-trip"
else
  echo "  SKIP sparse helpers (simg2img / img2simg not in this bundle)"
fi

echo "smoke test: OK"
