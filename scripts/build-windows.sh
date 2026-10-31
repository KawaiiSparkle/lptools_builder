#!/usr/bin/env bash
# Cross-compile the WINDOWS flavour of the lptools bundle (lpmake.exe,
# lpdump.exe, lpunpack.exe, lpadd.exe, simg2img.exe, img2simg.exe,
# append2simg.exe, ext2simg.exe) from the same minimal AOSP checkout.
#
# Why a separate script: upstream only enables the Windows host-cross target
# for lpmake. patches/ turns the rest on and fixes the portability bugs
# (O_BINARY, rename-over-existing, sysexits.h, asprintf, unix_io_manager).
# The affected projects must be clean before applying patches. Successful builds
# reverse only this patch set; failures keep it for inspection (--revert-only).
#
# Soong already ships the whole mechanism: the mingw-w64 toolchain lives in
# prebuilts/gcc/linux-x86/host/x86_64-w64-mingw32-4.8 and Windows host-cross
# variants are produced under out/host_cross/windows-<arch>/bin.
#
#   scripts/build-windows.sh                    # full build, default AOSP tag
#   scripts/build-windows.sh --skip-sync        # reuse an already synced tree
#   scripts/build-windows.sh --arch x86         # 32-bit (Soong's Windows default)
#   scripts/build-windows.sh --keep-patches     # leave the patches applied
#   scripts/build-windows.sh --revert-only      # undo a previous failed run
#   scripts/build-windows.sh --check-patches    # only verify the patch set
#                                               # still applies (drift sentinel)
#
# The .exe files cannot run on Linux, so the smoke test is skipped here. Verify
# the bundle on Windows with: imgpatchtools-rs super doctor
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/aosp-meta.sh
. "$ROOT/scripts/aosp-meta.sh"
# shellcheck source=scripts/log.sh
. "$ROOT/scripts/log.sh"
. "$ROOT/scripts/aosp-lunch.sh"
REF=${AOSP_REF:-android-15.0.0_r1}
PRODUCT=${LPTOOLS_PRODUCT:-}
WORK=${LPTOOLS_WORK:-$ROOT/aosp}
OUT=${LPTOOLS_OUT:-$ROOT/dist}
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
HOST_ARCH=x86_64
SKIP_SYNC=0
SYNC_ONLY=0
KEEP_PATCHES=0
REVERT_ONLY=0
CHECK_PATCHES=0
APPLIED=()
COMBO_FILE=""
HOST_BIN_FILE=""
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT
  set +e
  if declare -F cleanup >/dev/null 2>&1; then cleanup "$rc" || cleanup_rc=$?; fi
  if [ "$rc" -eq 0 ] && [ "$cleanup_rc" -ne 0 ]; then rc=$cleanup_rc; fi
  lp_finish "$rc"
  [ -z "$COMBO_FILE" ] || rm -f "$COMBO_FILE"
  [ -z "$HOST_BIN_FILE" ] || rm -f "$HOST_BIN_FILE"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

usage() { sed -n '2,/^set -euo/{/^set -euo/d;p}' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF=$2; shift 2 ;;
    --product) PRODUCT=$2; shift 2 ;;
    --work) WORK=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --jobs) JOBS=$2; shift 2 ;;
    --arch) HOST_ARCH=$2; shift 2 ;;
    --skip-sync) SKIP_SYNC=1; shift ;;
    --sync-only) SYNC_ONLY=1; shift ;;
    --keep-patches) KEEP_PATCHES=1; shift ;;
    --revert-only) REVERT_ONLY=1; shift ;;
    --check-patches) CHECK_PATCHES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$HOST_ARCH" in
  x86_64|amd64) HOST_ARCH=x86_64 ;;
  x86|i386|i686) HOST_ARCH=x86 ;;
  *) die "unsupported --arch $HOST_ARCH (use x86_64 or x86)" ;;
esac
PLATFORM="windows-$HOST_ARCH"


# --- the patch set ----------------------------------------------------------
# <project>:<patch file>; project paths are relative to the repo checkout.
PATCHES=(
  "system/extras:0001-system-extras-partition_tools-windows.patch"
  "system/core:0002-system-core-liblp-libsparse-windows.patch"
  "external/e2fsprogs:0003-external-e2fsprogs-ext2simg-windows.patch"
)
# Reverse exact hunks rather than checking out an entire project or touching index.
revert_project() {
  local entry=$1 proj="${1%%:*}" patch="$ROOT/patches/${1#*:}"
  if git -C "$WORK/$proj" apply --check --reverse "$patch" 2>/dev/null; then
    git -C "$WORK/$proj" apply --reverse "$patch"
  elif git -C "$WORK/$proj" apply --check "$patch" 2>/dev/null; then
    log "$proj already has the unpatched contents"
  else
    warn "cannot reverse $proj safely; inspect its changes manually"
    return 1
  fi
}

if [ "$REVERT_ONLY" -eq 1 ]; then
  [ -d "$WORK" ] || die "missing AOSP tree: $WORK"
  WORK=$(cd "$WORK" && pwd -P)
  log "reversing only the Windows patch set in $WORK"
  failed=0
  for entry in "${PATCHES[@]}"; do
    revert_project "$entry" || failed=1
  done
  exit "$failed"
fi

for bin in git python3 patch file; do
  command -v "$bin" >/dev/null 2>&1 || die "missing required tool: $bin"
done
if [ "$CHECK_PATCHES" -eq 0 ] && [ "$SYNC_ONLY" -eq 0 ]; then
  command -v zip >/dev/null 2>&1 || die "missing required tool: zip"
fi
for entry in "${PATCHES[@]}"; do
  [ -f "$ROOT/patches/${entry#*:}" ] || die "missing $ROOT/patches/${entry#*:}"
done

HOST_OS=$(uname -s | tr "[:upper:]" "[:lower:]")
[ "$HOST_OS" = "linux" ] || die "the mingw-w64 prebuilts are Linux x86 only; build this on Linux (or in CI), not on $HOST_OS"

mkdir -p "$WORK" "$OUT"
WORK=$(cd "$WORK" && pwd -P)
OUT=$(cd "$OUT" && pwd -P)
BUNDLE="$OUT/lptools-$PLATFORM"
TOTAL_START=$(now_s)

# --- 1. sync (delegated, so CI and local builds share one code path) --------
if [ "$SKIP_SYNC" -eq 0 ]; then
  log "syncing AOSP $REF into $WORK (via build-linux.sh --sync-only)"
  # The child owns its stage groups and heartbeat; keep its original exit code.
  bash "$ROOT/scripts/build-linux.sh" \
    --ref "$REF" --work "$WORK" --jobs "$JOBS" --sync-only
fi

if [ "$SYNC_ONLY" -eq 1 ]; then
  log "sync done (--sync-only); tree at $WORK"
  exit 0
fi

# --- 2. apply the Windows patches ------------------------------------------
apply_patches() {
  local entry proj patch
  # Validate all projects before changing any one of them. Index/worktree state
  # is left alone when there are pre-existing changes, including untracked files.
  for entry in "${PATCHES[@]}"; do
    proj="${entry%%:*}"
    [ -d "$WORK/$proj" ] || die "missing project $proj in $WORK"
    git -C "$WORK/$proj" rev-parse --git-dir >/dev/null
    [ -z "$(git -C "$WORK/$proj" status --porcelain --untracked-files=all)" ] \
      || die "$proj has local changes; save them before applying Windows patches"
  done
  for entry in "${PATCHES[@]}"; do
    proj="${entry%%:*}"
    patch="$ROOT/patches/${entry#*:}"
    log "patch ${entry#*:} -> $proj"
    if git -C "$WORK/$proj" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
      APPLIED+=("$entry")
      git -C "$WORK/$proj" apply --whitespace=nowarn "$patch"
    elif ( cd "$WORK/$proj" && patch -p1 --dry-run --forward --fuzz=3 --batch -i "$patch" >/dev/null 2>&1 ); then
      APPLIED+=("$entry")
      warn "${entry#*:} needs patch --fuzz=3"
      ( cd "$WORK/$proj" && patch -p1 --forward --fuzz=3 --batch --no-backup-if-mismatch -i "$patch" )
    else
      die "could not apply ${entry#*:} to $proj; regenerate it for $REF"
    fi
  done
}

cleanup() {
  local rc=$1 entry failed=0
  [ "${#APPLIED[@]}" -gt 0 ] || return 0
  if [ "$KEEP_PATCHES" -eq 1 ] || [ "$rc" -ne 0 ]; then
    warn "Windows patches kept for inspection: ${APPLIED[*]}"
    warn "to reverse them: bash scripts/build-windows.sh --work \"$WORK\" --revert-only"
    return 0
  fi
  for entry in "${APPLIED[@]}"; do
    log "reversing ${entry%%:*}"
    revert_project "$entry" || failed=1
  done
  APPLIED=()
  return "$failed"
}

# --- 2b. patch drift sentinel ----------------------------------------------
# Used by CI and by anyone bumping --ref: if an upstream branch moved (or the
# patch set was regenerated), find out in seconds instead of after a 40-minute
# build. Nothing is modified.
if [ "$CHECK_PATCHES" -eq 1 ]; then
  log "checking the Windows patch set against $WORK (read-only)"
  failed=0
  for entry in "${PATCHES[@]}"; do
    proj="${entry%%:*}"
    name="${entry#*:}"
    patch="$ROOT/patches/$name"
    [ -d "$WORK/$proj" ] || die "missing project $proj in $WORK (did the sync cover it?)"
    if git -C "$WORK/$proj" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
      log "  ok   $name -> $proj (git apply --check)"
    elif ( cd "$WORK/$proj" && patch -p1 --dry-run --forward --fuzz=3 --batch -i "$patch" >/dev/null 2>&1 ); then
      log "  ok   $name -> $proj (patch --dry-run, needs fuzz)"
    else
      warn "  FAIL $name -> $proj"
      failed=1
    fi
  done
  [ "$failed" -eq 0 ] || die "the patch set no longer applies cleanly;" \
      "regenerate it (git -C <project> diff > patches/<name>) before releasing."
  log "all patches apply to $WORK"
  exit 0
fi

group_start "apply the Windows patch set"
apply_patches
group_end

# --- 3. build with the official Soong build --------------------------------
[ -f "$WORK/build/envsetup.sh" ] || die "no AOSP build/envsetup.sh at $WORK"
COMBO_FILE=$(mktemp)
HOST_BIN_FILE=$(mktemp)
REQUIRED_EXES="lpmake lpdumps lpunpack"
OPTIONAL_EXES="lpadd simg2img img2simg append2simg ext2simg"

build_windows() {
  cd "$WORK"
  set +u
  source build/envsetup.sh
  lptools_lunch "${PRODUCT:-aosp_arm64}" || return $?
  local lunched="$LP_LUNCH_COMBO" t
  printf '%s\n' "$lunched" > "$COMBO_FILE"
  # OUT_DIR is a build-system setting; lunch may also set a shell variable OUT.
  local build_out="${OUT_DIR:-out}"
  local host_goal="$build_out/host_cross/windows-$HOST_ARCH/bin"
  local host_bin="$host_goal"
  case "$host_bin" in /*) ;; *) host_bin="$WORK/$host_bin" ;; esac
  printf '%s\n' "$host_bin" > "$HOST_BIN_FILE"
  local goals=()
  for t in $REQUIRED_EXES; do goals+=("$host_goal/$t.exe"); done
  if m -j"$JOBS" "${goals[@]}"; then :
  else
    warn "path goals failed; retrying with module names (HOST_CROSS_OS=windows)"
    export HOST_CROSS_OS=windows HOST_CROSS_ARCH="$HOST_ARCH"
    m -j"$JOBS" $REQUIRED_EXES
  fi
  for t in $OPTIONAL_EXES; do
    log "building optional $t.exe"
    m -j"$JOBS" "$host_goal/$t.exe" || warn "$t.exe is not available on $REF"
  done
}
group_start "build (setup + lunch + windows-$HOST_ARCH)"
run_beat "AOSP Windows build" build_windows

group_end

# --- 4. collect -------------------------------------------------------------
group_start "collect $PLATFORM binaries"
IFS= read -r BIN_SRC < "$HOST_BIN_FILE"
[ -d "$BIN_SRC" ] || die "no host_cross output at $BIN_SRC"
log "collecting Windows binaries from $BIN_SRC"

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/bin"

missing=""
for t in $REQUIRED_EXES $OPTIONAL_EXES; do
  if [ -f "$BIN_SRC/$t.exe" ]; then
    description=$(file -b "$BIN_SRC/$t.exe")
    case "$HOST_ARCH:$description" in
      x86_64:*PE32+*x86-64*|x86:*PE32*Intel*80386*) ;;
      *) die "$t.exe architecture does not match $PLATFORM: $description" ;;
    esac
    cp -p "$BIN_SRC/$t.exe" "$BUNDLE/bin/$t.exe"
  else
    case " $REQUIRED_EXES " in
      *" $t "*) missing="$missing $t.exe" ;;
      *) warn "$t.exe not built by this AOSP revision" ;;
    esac
  fi
done
[ -z "$missing" ] || die "required binaries not built:$missing"

# lpdumps is the Windows-safe twin of lpdump (same lpdump.cc, no liblpdump
# shared-library chain). Ship it under the name every consumer expects.
if [ -f "$BUNDLE/bin/lpdumps.exe" ]; then
  mv "$BUNDLE/bin/lpdumps.exe" "$BUNDLE/bin/lpdump.exe"
fi

( cd "$BUNDLE/bin" && file ./*.exe 2>/dev/null || true )
group_end

# --- 5. manifest + package --------------------------------------------------
group_start "package (manifest + zip)"
log "recording AOSP provenance (branch / tag / pinned manifest)"
lptools_export_meta "$WORK" "$BUNDLE"
REPO_VERSION=$(repo --version 2>/dev/null | awk "/repo version/ {print \$3}" || true)
BUILDER_SHA=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
BUILDER_REF=$(git -C "$ROOT" remote get-url origin 2>/dev/null || echo unknown)
BUILDER_BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
export SOURCE_DATE_EPOCH_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

log "writing manifest (AOSP branch: ${AOSP_BRANCH:-unknown})"
python3 "$ROOT/scripts/make-manifest.py" \
  --dir "$BUNDLE" \
  --aosp-ref "$REF" \
  --aosp-branch "$AOSP_BRANCH" \
  --aosp-tag "$AOSP_TAG" \
  --product "$(cat "$COMBO_FILE" 2>/dev/null || echo unknown)" \
  --aosp-manifest-rev "$AOSP_MANIFEST_REV" \
  --aosp-manifest-url "https://android.googlesource.com/platform/manifest/+/$AOSP_MANIFEST_REV" \
  --aosp-pinned-projects "$AOSP_PINNED_PROJECTS" \
  --builder-sha "$BUILDER_SHA" \
  --builder-branch "$BUILDER_BRANCH" \
  --builder-ref "$BUILDER_REF" \
  --builder-run "${GITHUB_RUN_ID:-}" \
  --repo-version "$REPO_VERSION" \
  --platform "$PLATFORM" \
  --bundle-version "$REF"

log "packaging zip"
ZIP="lptools-$PLATFORM.zip"
rm -f "$OUT/$ZIP"
( cd "$OUT" && zip -X -9 -r -q "$ZIP" "lptools-$PLATFORM" )
printf '%s  %s\n' "$(lptools_sha256 "$OUT/$ZIP")" "$ZIP" > "$OUT/$ZIP.sha256"

group_end

log "done in $( _fmt_dur $(( $(now_s) - TOTAL_START )) ): $OUT/$ZIP"
cat "$BUNDLE/manifest.json"
