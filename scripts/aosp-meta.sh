#!/usr/bin/env bash
# Shared helper: record exactly which AOSP tree a bundle was built from.
#
# Every build script sources this file so the host (linux/darwin), Windows
# host-cross and Android-target flavours describe their source identically.
#
#   branch  -- the branch the manifest itself tracked (android15-release, main,
#              ...). This is what people mean by "which branch was fetched".
#   tag     -- the exact tag the manifest revision carries, when it is a tag.
#   rev     -- commit of the platform/manifest repository (the manifest metadata
#              itself, not the projects it points at).
#   pinned  -- "repo manifest -r" output: every project pinned to one SHA-1.
#              It ships inside the bundle as aosp-manifest.xml, so a bundle can
#              be rebuilt from exactly the recorded sources.

# lptools_aosp_branch <work>: the branch the manifest followed.
lptools_aosp_branch() {
  local work=$1 branch=""
  if [ -f "$work/.repo/manifest.xml" ]; then
    branch=$(sed -n 's/.*<default[^>]*revision="\([^"]*\)".*/\1/p' "$work/.repo/manifest.xml" | head -n1 || true)
  fi
  if [ -z "$branch" ] && [ -d "$work/.repo/manifests" ]; then
    branch=$(git -C "$work/.repo/manifests" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    [ "$branch" = "HEAD" ] && branch=""
  fi
  printf '%s' "$branch"
}

# lptools_aosp_tag <work>: tag at the manifest revision, empty when untagged.
lptools_aosp_tag() {
  local work=$1
  [ -d "$work/.repo/manifests" ] || return 0
  git -C "$work/.repo/manifests" describe --tags --exact-match HEAD 2>/dev/null || true
}

# lptools_aosp_revision <work>: commit of the manifest repository itself.
lptools_aosp_revision() {
  local work=$1
  [ -d "$work/.repo/manifests" ] || return 0
  git -C "$work/.repo/manifests" rev-parse HEAD 2>/dev/null || true
}

# lptools_write_pinned_manifest <work> <bundle>: write <bundle>/aosp-manifest.xml
# and print the number of pinned projects (0 when there is no .repo metadata).
lptools_write_pinned_manifest() {
  local work=$1 bundle=$2 out="$2/aosp-manifest.xml" count=""
  if [ -d "$work/.repo" ] && command -v repo >/dev/null 2>&1; then
    if ( cd "$work" && repo manifest -r -o "$out" >/dev/null 2>&1 ); then
      count=$(grep -c '<project ' "$out" 2>/dev/null || true)
      count=$(printf '%s' "$count" | tr -cd '0-9')
      printf '%s' "${count:-0}"
      return 0
    fi
  fi
  printf '0'
}

# lptools_export_meta <work> <bundle>: sets AOSP_BRANCH / AOSP_TAG /
# AOSP_MANIFEST_REV / AOSP_PINNED_PROJECTS for the caller.
lptools_export_meta() {
  local work=$1 bundle=$2
  AOSP_BRANCH=$(lptools_aosp_branch "$work")
  AOSP_TAG=$(lptools_aosp_tag "$work")
  AOSP_MANIFEST_REV=$(lptools_aosp_revision "$work")
  AOSP_PINNED_PROJECTS=$(lptools_write_pinned_manifest "$work" "$bundle")
  AOSP_BRANCH=${AOSP_BRANCH:-}
  AOSP_TAG=${AOSP_TAG:-}
  AOSP_MANIFEST_REV=${AOSP_MANIFEST_REV:-}
  AOSP_PINNED_PROJECTS=${AOSP_PINNED_PROJECTS:-0}
}

# lptools_sha256 <file>: portable SHA-256 (GNU coreutils, BSD shasum, openssl).
lptools_sha256() {
  local f=$1
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$f" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$f" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$f" | sed 's/.*= //'
  else return 1
  fi
}

# lptools_make_tarball <outdir> <bundle-name>: deterministic .tar.gz.
# GNU tar can pin mtime/owner and sort entries; bsdtar (macOS) cannot, so it
# falls back to a plain archive -- same contents, slightly different metadata.
lptools_make_tarball() {
  local out=$1 name=$2
  if tar --version 2>/dev/null | grep -q "GNU tar"; then
    ( cd "$out" && tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner \
        -cf - "$name" | gzip -9n > "$name.tar.gz" )
  else
    ( cd "$out" && tar -cf - "$name" | gzip -9n > "$name.tar.gz" )
  fi
}
