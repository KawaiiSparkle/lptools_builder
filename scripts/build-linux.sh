#!/usr/bin/env bash
# Build the lptools bundle (lpmake / lpdump / lpunpack [+ lpadd / lpflash] +
# simg2img / img2simg) from a minimal AOSP checkout, verify it, and package it.
#
# This is the single source of truth for the build: CI (.github/workflows/
# build-lptools.yml) calls it, and so do local builds on Linux, WSL2 and macOS.
#
#   scripts/build-linux.sh                       # full build, default AOSP tag
#   scripts/build-linux.sh --ref android-16.0.0_r2
#   scripts/build-linux.sh --skip-sync            # reuse an existing checkout
#   scripts/build-linux.sh --sync-only            # just fetch the tree
#   scripts/build-linux.sh --work /mnt/aosp --out dist
#   scripts/build-linux.sh --arch arm64           # require this native host architecture
#   scripts/build-linux.sh --project-list config/projects-latest.txt
#
# Platforms: linux-x86_64, linux-arm64, darwin-x86_64, darwin-arm64 (the bundle
# gets the name of the HOST it was built on or for). On macOS the tree must be
# on a case-sensitive volume.
#
# Requirements: git, python3, curl, and ~30 GB of free disk for a cold tree.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
. "$ROOT/scripts/aosp-meta.sh"
. "$ROOT/scripts/log.sh"
. "$ROOT/scripts/aosp-lunch.sh"
REF=${AOSP_REF:-android-15.0.0_r1}
PRODUCT=${LPTOOLS_PRODUCT:-}
WORK=${LPTOOLS_WORK:-$ROOT/aosp}
OUT=${LPTOOLS_OUT:-$ROOT/dist}
JOBS=${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}
SKIP_SYNC=0
SYNC_ONLY=0
NO_SMOKE=0
ARCH_ARG=
PROJECT_LIST="$ROOT/required-projects.txt"
COMBO_FILE=""
HOST_BIN_FILE=""
PROJECTS_FILE=""
finish() {
  local rc=$?
  trap - EXIT
  set +e
  lptools_cleanup_host_product
  lp_finish "$rc"
  [ -z "$COMBO_FILE" ] || rm -f "$COMBO_FILE"
  [ -z "$HOST_BIN_FILE" ] || rm -f "$HOST_BIN_FILE"
  [ -z "$PROJECTS_FILE" ] || rm -f "$PROJECTS_FILE"
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
    --arch) ARCH_ARG=$2; shift 2 ;;
    --project-list) PROJECT_LIST=$2; shift 2 ;;
    --skip-sync) SKIP_SYNC=1; shift ;;
    --sync-only) SYNC_ONLY=1; shift ;;
    --no-smoke) NO_SMOKE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for bin in git python3 curl tar gzip file; do
  command -v "$bin" >/dev/null 2>&1 || die "missing required tool: $bin"
done

if ! command -v repo >/dev/null 2>&1; then
  die "repo is not installed. Install it with:" \
      "  mkdir -p ~/.local/bin && curl -sSL https://storage.googleapis.com/git-repo-downloads/repo -o ~/.local/bin/repo && chmod +x ~/.local/bin/repo" \
      "  export PATH=~/.local/bin:$PATH"
fi

# --- host + target platform -------------------------------------------------
HOST_OS=$(uname -s | tr "[:upper:]" "[:lower:]")
case "$HOST_OS" in
  linux|darwin) ;;
  *) die "unsupported host: $HOST_OS (use Linux, WSL2 or macOS)" ;;
esac

ARCH="$ARCH_ARG"
[ -n "$ARCH" ] || ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported architecture: $ARCH (use x86_64 or arm64)" ;;
esac
PLATFORM="$HOST_OS-$ARCH"
NATIVE_ARCH=$(uname -m)
case "$NATIVE_ARCH" in
  amd64|x86_64) NATIVE_ARCH=x86_64 ;;
  aarch64|arm64) NATIVE_ARCH=arm64 ;;
esac
if [ "$SYNC_ONLY" -eq 0 ] && [ "$ARCH" != "$NATIVE_ARCH" ]; then
  die "--arch $ARCH does not match host $NATIVE_ARCH; this script does not configure cross-compilation"
fi

# AOSP requires a case-sensitive filesystem on macOS; catch it here instead
# of in the middle of the build.
if [ "$HOST_OS" = "darwin" ]; then
  mkdir -p "$WORK"
  probe="$WORK/.__lptools_case_probe"
  : > "$probe"
  if [ -e "$WORK/.__LPTOOLS_CASE_PROBE" ]; then
    rm -f "$probe"
    die "$WORK is on a case-insensitive volume; the AOSP build needs a" \
        "case-sensitive one on macOS. Create one with:" \
        "  hdiutil create -type SPARSE -fs 'Case-sensitive APFS' -size 60g -volname aosp lptools.dmg" \
        "  hdiutil attach lptools.dmg -mountpoint /Volumes/aosp" \
        "and pass --work /Volumes/aosp/aosp"
  fi
  rm -f "$probe"
fi

# Resolve paths before any subshell changes its working directory.
mkdir -p "$WORK" "$OUT"
WORK=$(cd "$WORK" && pwd -P)
OUT=$(cd "$OUT" && pwd -P)

# --- disk space -------------------------------------------------------------
avail_kb=$(df -Pk "$WORK" | awk 'NR==2 {print $4}')
avail_gb=$((avail_kb / 1024 / 1024))
if [ "$SKIP_SYNC" -eq 0 ] && [ "$avail_gb" -lt 30 ]; then
  warn "only ${avail_gb} GB free next to $WORK; a cold minimal checkout + build needs ~30 GB."
  warn "On a hosted runner, run scripts/free-disk.sh (Linux) or free-disk-darwin.sh first."
fi

TOTAL_START=$(now_s)

# --- 1. manifest metadata ---------------------------------------------------
if [ "$SKIP_SYNC" -eq 0 ]; then
  group_start "init + validate (${REF})"
  log "repo init: $REF (refresh the requested manifest ref even on a reused tree)"
  # A build checks out source blobs; avoid deferred per-blob fetches. Actual
  # performance depends on the network and the repo/Git versions in use.
  ( cd "$WORK" && run_beat "repo init" repo init \
      -u https://android.googlesource.com/platform/manifest -b "$REF" --depth=1 )

  # Use the validated, actual manifest names for sync as well as validation.
  log "resolving $PROJECT_LIST against the manifest"
  PROJECTS_FILE=$(mktemp)
  python3 "$ROOT/tools/check_projects.py" --repo-root "$WORK" --list "$PROJECT_LIST" \
    --print-projects > "$PROJECTS_FILE" \
    || die "project list does not match this manifest; fix it or pick another --ref"
  PROJECTS=()
  while IFS= read -r project; do
    [ -z "$project" ] || PROJECTS+=("$project")
  done < "$PROJECTS_FILE"
  [ "${#PROJECTS[@]}" -gt 0 ] || die "no projects resolved from required-projects.txt"
  group_end
fi

# --- 2. sync only what we need ----------------------------------------------
if [ "$SKIP_SYNC" -eq 0 ]; then
  group_start "sync (${REF})"
  log "repo sync: ${#PROJECTS[@]} resolved projects (-j$JOBS)"
  ( cd "$WORK" && run_beat "repo sync" repo sync -c -j"$JOBS" --no-tags \
      --optimized-fetch --prune --fail-fast "${PROJECTS[@]}" )

  group_end
fi

if [ "$SYNC_ONLY" -eq 1 ]; then
  log "sync done (--sync-only); tree at $WORK"
  exit 0
fi

# --- 3. build with the official Soong build --------------------------------
[ -f "$WORK/build/envsetup.sh" ] || die "no AOSP build/envsetup.sh at $WORK"
lptools_prepare_host_product "$WORK" "$ROOT/config/host-product" "${PRODUCT:-lptools_host}"
COMBO_FILE=$(mktemp)
HOST_BIN_FILE=$(mktemp)

# source, lunch and m share one shell; the heartbeat covers initialization too.
build_host() {
  cd "$WORK"
  # AOSP's environment scripts expect unset variables to expand to empty.
  set +u
  source build/envsetup.sh
  lptools_host_lunch "$PRODUCT" || return $?
  local lunched="$LP_LUNCH_COMBO"
  printf '%s\n' "$lunched" > "$COMBO_FILE"
  # Soong host installations use OUT_DIR/host, including --soong-only builds.
  local install_arch="$ARCH" build_out="${OUT_DIR:-out}" host_bin
  [ "$install_arch" != x86_64 ] || install_arch=x86
  local host_goal="$build_out/host/$HOST_OS-$install_arch/bin"
  host_bin="$host_goal"
  case "$host_bin" in /*) ;; *) host_bin="$WORK/$host_bin" ;; esac
  printf '%s\n' "$host_bin" > "$HOST_BIN_FILE"

  # Module-name phony targets include every variant (including Android lpdump).
  # Request only the native host installation, preserving relative Ninja paths.
  local tool goals=()
  for tool in lpmake lpdump lpunpack simg2img img2simg; do
    goals+=("$host_goal/$tool")
  done
  m --soong-only -j"$JOBS" "${goals[@]}"
  local extra
  for extra in lpadd lpflash; do
    log "building optional $extra"
    m --soong-only -j"$JOBS" "$host_goal/$extra" || warn "$extra is not available on $REF"
  done
}
group_start "build (setup + lunch + m -j$JOBS)"
run_beat "AOSP host build" build_host

group_end

# --- 4. collect -------------------------------------------------------------
group_start "collect binaries"
IFS= read -r BIN_SRC < "$HOST_BIN_FILE"
[ -d "$BIN_SRC" ] || die "no host output directory at $BIN_SRC"
log "collecting host binaries from ${BIN_SRC#"$WORK"/}"

BUNDLE="$OUT/lptools-$PLATFORM"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/bin"

missing=""
for t in lpmake lpdump lpunpack simg2img img2simg lpadd lpflash; do
  if [ -x "$BIN_SRC/$t" ]; then
    description=$(file -b "$BIN_SRC/$t")
    case "$ARCH:$description" in
      x86_64:*x86-64*|x86_64:*x86_64*|arm64:*aarch64*|arm64:*arm64*) ;;
      *) die "$t architecture does not match $PLATFORM: $description" ;;
    esac
    cp -p "$BIN_SRC/$t" "$BUNDLE/bin/$t"
  else
    case "$t" in
      lpadd|lpflash) warn "$t not built by this AOSP revision" ;;
      *) missing="$missing $t" ;;
    esac
  fi
done
[ -z "$missing" ] || die "required binaries not built:$missing"

# Soong host binaries use $ORIGIN/../lib[64] (or @loader_path on Darwin).
# Include the installed runtime tree, including transitive shared dependencies.
# Dereference Soong installation symlinks so the archive is self-contained.
for libdir in lib64 lib; do
  if [ -d "$BIN_SRC/../$libdir" ]; then
    log "collecting host runtime $libdir"
    mkdir -p "$BUNDLE/$libdir"
    cp -R -L -p "$BIN_SRC/../$libdir/." "$BUNDLE/$libdir/"
  fi
done

( cd "$BUNDLE/bin" && file * 2>/dev/null || true )
group_end

# --- 5. verify --------------------------------------------------------------
if [ "$NO_SMOKE" -eq 0 ]; then
  group_start "verify (lpmake -> lpdump -> lpunpack round trip)"
  run_beat "smoke test" bash "$ROOT/scripts/smoke-test.sh" "$BUNDLE/bin"
  group_end
fi

# --- 6. manifest + package --------------------------------------------------
group_start "package (manifest + tarball)"
log "recording AOSP provenance (branch / tag / pinned manifest)"
lptools_export_meta "$WORK" "$BUNDLE" "$REF"
REPO_VERSION=$(repo --version 2>/dev/null | awk "/repo version/ {print \$3}" || true)
# A self-contained CI payload has no Git checkout; the runner supplies its source identity.
BUILDER_SHA=${LPTOOLS_BUILDER_SHA:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}
BUILDER_REF=${LPTOOLS_BUILDER_REF:-$(git -C "$ROOT" remote get-url origin 2>/dev/null || echo unknown)}
BUILDER_BRANCH=${LPTOOLS_BUILDER_BRANCH:-$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}
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

log "packaging"
lptools_make_tarball "$OUT" "lptools-$PLATFORM"
printf '%s  %s\n' "$(lptools_sha256 "$OUT/lptools-$PLATFORM.tar.gz")" \
  "lptools-$PLATFORM.tar.gz" > "$OUT/lptools-$PLATFORM.tar.gz.sha256"

group_end

log "done in $( _fmt_dur $(( $(now_s) - TOTAL_START )) ): $OUT/lptools-$PLATFORM.tar.gz"
cat "$BUNDLE/manifest.json"
