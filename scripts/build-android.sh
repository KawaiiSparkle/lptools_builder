#!/usr/bin/env bash
# Build the lptools bundle for Android DEVICES (bionic, arm64/arm/x86_64/x86).
#
# The selected tree must provide device variants of the required tools. Stock
# AOSP tags may define lpmake/lpunpack only as host tools; this script does not
# convert their Soong modules and fails if the device outputs are missing.
#
#   scripts/build-android.sh                    # arm64 + arm (the usual pair)
#   scripts/build-android.sh --abi arm64,x86_64
#   scripts/build-android.sh --skip-sync        # reuse an already synced tree
#
# Direct shared-library dependencies found in the product's system/lib* are
# copied into lib/ on a best-effort basis. Transitive dependencies and runtime
# compatibility still need device validation. Use --no-libs to skip collection.
#
# Requires a synced AOSP tree (this script can do it) and ~40 GB of disk.
set -euo pipefail

BUILD_TMP=
android_finish() {
  local rc=$?
  trap - EXIT
  # The trap also covers failures before the shared logger has been sourced.
  if declare -F lp_finish >/dev/null 2>&1; then lp_finish "$rc" || true; fi
  if [ -n "$BUILD_TMP" ]; then rm -rf -- "$BUILD_TMP" || true; fi
  exit "$rc"
}
trap android_finish EXIT

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/aosp-meta.sh
. "$ROOT/scripts/aosp-meta.sh"
# shellcheck source=scripts/log.sh
. "$ROOT/scripts/log.sh"
REF=${AOSP_REF:-android-15.0.0_r1}
ABIS=${LPTOOLS_ABIS:-arm64,arm}
WORK=${LPTOOLS_WORK:-$ROOT/aosp}
OUT=${LPTOOLS_OUT:-$ROOT/dist}
JOBS=${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}
SKIP_SYNC=0
COLLECT_LIBS=1
NO_SMOKE=1

usage() { sed -n '2,/^set -euo/{/^set -euo/d;p}' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF=$2; shift 2 ;;
    --abi) ABIS=$2; shift 2 ;;
    --work) WORK=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --jobs) JOBS=$2; shift 2 ;;
    --skip-sync) SKIP_SYNC=1; shift ;;
    --no-libs) COLLECT_LIBS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for bin in git python3 tar gzip readelf; do
  command -v "$bin" >/dev/null 2>&1 || die "missing required tool: $bin (binutils provides readelf)"
done

case "$(uname -s)" in
  Linux) ;;
  *) die "build Android targets on Linux" ;;
esac

mkdir -p "$WORK" "$OUT"
# All later subshells and metadata helpers may change directory.
WORK=$(cd "$WORK" && pwd -P)
OUT=$(cd "$OUT" && pwd -P)
TOTAL_START=$(now_s)

if [ "$SKIP_SYNC" -eq 0 ]; then
  log "syncing AOSP $REF into $WORK (via build-linux.sh --sync-only)"
  # The child owns its sync groups and heartbeat; preserve its exit status.
  bash "$ROOT/scripts/build-linux.sh" \
    --ref "$REF" --work "$WORK" --jobs "$JOBS" --sync-only
fi
[ -f "$WORK/build/envsetup.sh" ] || die "no AOSP tree at $WORK (pass --skip-sync only if it is already synced)"

# ABI -> (product, lib dirs)
abi_product() {
  case "$1" in
    arm64)  echo aosp_arm64 ;;
    arm)    echo aosp_arm ;;
    x86_64) echo aosp_x86_64 ;;
    x86)    echo aosp_x86 ;;
    *) die "unknown --abi $1 (use arm64, arm, x86_64, x86)" ;;
  esac
}
abi_libdirs() {
  case "$1" in
    arm64|x86_64) echo "system/lib64 system/lib" ;;
    *)            echo "system/lib system/lib64" ;;
  esac
}

MODULES="lpmake lpdump lpunpack lpadd simg2img img2simg"
# Libraries we do not ship: they are part of every Android runtime.
SKIP_LIBS="libc.so libm.so libdl.so libstdc++.so liblog.so libcutils.so libandroid.so"

REPO_VERSION=$(repo --version 2>/dev/null | awk "/repo version/ {print \$3}" || true)
BUILDER_SHA=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
BUILDER_REF=$(git -C "$ROOT" remote get-url origin 2>/dev/null || echo unknown)
BUILDER_BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
export SOURCE_DATE_EPOCH_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

BUILD_TMP=$(mktemp -d)
LUNCH_LOG="$BUILD_TMP/lunch.log"

# run_beat isolates this entire function. lunch must run in the same shell as m:
# it changes the build environment, and both envsetup and lunch can take time.
build_android() {
  local product=$1 candidate lunched="" rc=1 product_out
  cd "$WORK" || return $?
  # AOSP's shell helpers are not nounset-safe. This only affects the build child.
  set +u
  source build/envsetup.sh >/dev/null || return $?
  for candidate in "$product-eng" "$product-trunk_staging-eng"; do
    if lunch "$candidate" >"$LUNCH_LOG" 2>&1; then
      lunched=$candidate
      break
    else
      rc=$?
    fi
  done
  if [ -z "$lunched" ]; then
    tail -20 "$LUNCH_LOG" >&2 || true
    warn "lunch failed for $product (tried -eng and -trunk_staging-eng)"
    return "$rc"
  fi
  log "lunch $lunched"
  # lunch exports the absolute PRODUCT_OUT as ANDROID_PRODUCT_OUT, including
  # OUT_DIR overrides. get_abs_build_var is no longer public in Android 15.
  product_out=${ANDROID_PRODUCT_OUT:-}
  [ -n "$product_out" ] || { warn "empty ANDROID_PRODUCT_OUT for $lunched"; return 1; }
  printf '%s\n' "$lunched" > "$BUILD_TMP/combo" || return $?
  printf '%s\n' "$product_out" > "$BUILD_TMP/product-out" || return $?
  m -j"$JOBS" $MODULES
}

for abi in $(echo "$ABIS" | tr "," " "); do
  product=$(abi_product "$abi")
  group_start "build android-$abi (product $product)"
  run_beat "envsetup + lunch + compile for $product" build_android "$product"
  IFS= read -r lunched < "$BUILD_TMP/combo"
  IFS= read -r PRODUCT_OUT < "$BUILD_TMP/product-out"

  BIN_SRC="$PRODUCT_OUT/system/bin"
  [ -d "$BIN_SRC" ] || die "no device binaries for $lunched at $BIN_SRC; the selected tree must provide Android variants"

  BUNDLE="$OUT/lptools-android-$abi"
  rm -rf "$BUNDLE"
  mkdir -p "$BUNDLE/bin"

  missing=""
  for t in $MODULES; do
    if [ -f "$BIN_SRC/$t" ]; then
      cp -p "$BIN_SRC/$t" "$BUNDLE/bin/$t"
    else
      case " lpmake lpdump lpunpack " in
        *" $t "*) missing="$missing $t" ;;
        *) warn "$t not built for $product" ;;
      esac
    fi
  done
  [ -z "$missing" ] || die "required Android binaries missing for $abi:$missing; host-only Soong modules do not produce device binaries"

  if [ "$COLLECT_LIBS" -eq 1 ]; then
    mkdir -p "$BUNDLE/lib"
    for t in $MODULES; do
      [ -f "$BUNDLE/bin/$t" ] || continue
      readelf -d "$BUNDLE/bin/$t" 2>/dev/null | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p'
    done | sort -u | while read -r lib; do
      [ -n "$lib" ] || continue
      case " $SKIP_LIBS " in *" $lib "*) continue ;; esac
      for dir in $(abi_libdirs "$abi"); do
        if [ -f "$PRODUCT_OUT/$dir/$lib" ]; then
          cp -p "$PRODUCT_OUT/$dir/$lib" "$BUNDLE/lib/" 2>/dev/null || true
          break
        fi
      done
      [ -f "$BUNDLE/lib/$lib" ] || : # system-provided, ignore
    done
    # Record what we could not ship so nobody wonders later.
    : > "$BUNDLE/lib/README.txt"
    printf '%s\n' "Run with:" "  LD_LIBRARY_PATH=<bundle>/lib ./bin/lpunpack super.img out" \
      "" "Libraries listed in bin/* DT_NEEDED that are absent here are expected to" \
      "come from the device (/system/lib64 or /system/lib)." > "$BUNDLE/lib/README.txt"
  fi

  group_end
  group_start "package android-$abi"
  lptools_export_meta "$WORK" "$BUNDLE"

  log "writing manifest for android-$abi (AOSP branch: ${AOSP_BRANCH:-unknown})"
  python3 "$ROOT/scripts/make-manifest.py" \
    --dir "$BUNDLE" \
    --aosp-ref "$REF" \
    --aosp-branch "$AOSP_BRANCH" \
    --aosp-tag "$AOSP_TAG" \
    --product "$lunched" \
    --aosp-manifest-rev "$AOSP_MANIFEST_REV" \
    --aosp-manifest-url "https://android.googlesource.com/platform/manifest/+/$AOSP_MANIFEST_REV" \
    --aosp-pinned-projects "$AOSP_PINNED_PROJECTS" \
    --builder-sha "$BUILDER_SHA" \
    --builder-branch "$BUILDER_BRANCH" \
    --builder-ref "$BUILDER_REF" \
    --builder-run "${GITHUB_RUN_ID:-}" \
    --repo-version "$REPO_VERSION" \
    --platform "android-$abi" \
    --bundle-version "$REF"

  lptools_make_tarball "$OUT" "lptools-android-$abi"
  printf '%s  %s\n' "$(lptools_sha256 "$OUT/lptools-android-$abi.tar.gz")" \
    "lptools-android-$abi.tar.gz" > "$OUT/lptools-android-$abi.tar.gz.sha256"
  log "packaged: lptools-android-$abi.tar.gz"
  group_end
done

log "done in $( _fmt_dur $(( $(now_s) - TOTAL_START )) )"
ls -lh "$OUT"/lptools-android-*.tar.gz
