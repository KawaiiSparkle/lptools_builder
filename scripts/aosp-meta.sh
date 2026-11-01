#!/usr/bin/env bash
# Shared helper: record exactly which AOSP tree a bundle was built from.
#
# Every build script sources this file so the host (linux/darwin), Windows
# host-cross and Android-target flavours describe their source identically.
#
#   branch  -- the branch the manifest itself tracked (android15-release, main,
#              ...), resolved from the merged manifest's <default revision=...>
#              and from repo's own record of what init fetched
#              (.repo/manifests branch.default.merge). Never the synthetic
#              local branch name repo always gives that checkout ("default").
#   tag     -- the exact tag the manifest revision carries, when it is a tag.
#              Kept even when the fetch did not create the local tag ref.
#   rev     -- commit of the platform/manifest repository (the manifest metadata
#              itself, not the projects it points at).
#   pinned  -- a pinned manifest naming only the projects this build actually
#              synced, each pinned to the commit that is checked out. It ships
#              inside the bundle as aosp-manifest.xml, so a bundle can be
#              traced back to the recorded project commits.
#
# Everything here fails loudly (nonzero return, so the caller's "set -e" aborts
# the build) instead of shipping an empty or invented manifest: a bundle
# without trustworthy provenance is worse than no bundle.

# lptools_aosp_die <message...>: report a provenance failure and return nonzero.
lptools_aosp_die() {
  local msg="aosp-meta: $*"
  if declare -F warn >/dev/null 2>&1; then
    warn "$msg"
  else
    printf '%s\n' "$msg" >&2
  fi
  return 1
}

# lptools_aosp_note <message...>: progress line through the caller's log().
lptools_aosp_note() {
  if declare -F log >/dev/null 2>&1; then
    log "aosp-meta: $*"
  fi
  return 0
}

# lptools_aosp_merged_manifest <work> <out> <errlog>: write the manifest repo
# merged with its includes and .repo/local_manifests/*.xml.
#
# Deliberately no -r/--revision-as-HEAD: that mode resolves every project's
# checkout (project.work_git.ResolveCommit(HEAD)) and aborts the whole export
# when any project was never synced -- exactly the partial-sync case this
# pipeline builds. Pinning is done per synced project further down instead.
lptools_aosp_merged_manifest() {
  local work=$1 out=$2 errlog=$3
  if [ -z "$work" ] || [ -z "$out" ]; then
    lptools_aosp_die "lptools_aosp_merged_manifest needs a work tree and an output file"
    return 1
  fi
  if [ ! -d "$work/.repo" ]; then
    lptools_aosp_die "no repo metadata at $work/.repo; refusing to record empty AOSP provenance"
    return 1
  fi
  if ! command -v repo >/dev/null 2>&1; then
    lptools_aosp_die "'repo' is not on PATH; cannot export the merged manifest of $work"
    return 1
  fi
  rm -f -- "$out"
  if ! ( cd "$work" && repo manifest -o "$out" ) >/dev/null 2>"$errlog"; then
    lptools_aosp_die "repo manifest failed in $work: $(tr '\n' ' ' <"$errlog" 2>/dev/null | tail -c 400)"
    return 1
  fi
  if [ ! -s "$out" ]; then
    lptools_aosp_die "repo manifest wrote an empty manifest for $work"
    return 1
  fi
  return 0
}

# lptools_aosp_default_revision <merged-manifest>: the manifest's own
# <default revision=...>, empty when the manifest does not set one.
lptools_aosp_default_revision() {
  local merged=$1
  [ -s "$merged" ] || return 1
  python3 - "$merged" <<'LPTOOLS_REVISION'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
default = root.find("default")
print(default.get("revision", "") if default is not None else "", end="")
LPTOOLS_REVISION
}

# lptools_aosp_resolve_ref <work> [default-revision] [requested-ref]:
# print "<branch><TAB><tag>" for this tree.
#
# Evidence order, no guessing:
#   1. the merged manifest's <default revision=...> (covers a wrapper manifest
#      or local_manifests override that supplies the real revision);
#   2. branch.default.merge in the manifest checkout -- the full ref repo init
#      actually fetched. Its refs/heads or refs/tags prefix says what kind of
#      ref it was, which is how a tag stays a tag when a -c/--no-tags fetch
#      left no local tag ref behind;
#   3. for a bare name, the manifest repo's own refs;
#   4. the caller's requested ref, when the tree carries no evidence at all;
#   5. otherwise nothing: branch and tag stay empty and the gap is reported.
lptools_aosp_resolve_ref() {
  local work=$1 defrev=${2:-} requested=${3:-}
  local mdir="$1/.repo/manifests" merge="" kind="" cand="" from_merge=0
  if [ -d "$mdir" ]; then
    merge=$(git -C "$mdir" config --get branch.default.merge 2>/dev/null || true)
  fi
  case $merge in
    refs/tags/*) kind=tag; merge=${merge#refs/tags/} ;;
    refs/heads/*) kind=branch; merge=${merge#refs/heads/} ;;
    refs/remotes/*) kind=branch; merge=${merge##*/} ;;
    refs/*) merge=${merge#refs/} ;;
  esac
  # A manifest synced at a fixed commit records a SHA there, not a ref: a commit
  # is neither a branch nor a tag, so keep it out of both fields.
  if [[ $merge =~ ^[0-9a-f]{40}$ ]]; then merge=""; fi
  [ "$merge" = "HEAD" ] && merge=""

  cand=$defrev
  if [ -z "$cand" ]; then
    cand=$merge
    from_merge=1
  fi
  if [ -z "$cand" ] && [ -n "$requested" ]; then
    cand=$requested
  fi
  if [ -z "$cand" ]; then
    printf '\t'
    return 0
  fi
  case $cand in
    refs/tags/*) printf '\t%s' "${cand#refs/tags/}"; return 0 ;;
    refs/heads/*) printf '%s\t' "${cand#refs/heads/}"; return 0 ;;
    refs/remotes/*) printf '%s\t' "${cand##*/}"; return 0 ;;
    refs/*) cand=${cand#refs/} ;;
  esac
  [ "$cand" = "$merge" ] && from_merge=1

  if [ "$from_merge" -eq 1 ] && [ "$kind" = "tag" ]; then
    printf '\t%s' "$cand"
    return 0
  fi
  if [ "$from_merge" -eq 1 ] && [ "$kind" = "branch" ]; then
    printf '%s\t' "$cand"
    return 0
  fi
  if [ -d "$mdir" ] && git -C "$mdir" rev-parse --verify --quiet "refs/tags/$cand^{commit}" >/dev/null 2>&1; then
    printf '\t%s' "$cand"
    return 0
  fi
  if [ -d "$mdir" ] && { git -C "$mdir" rev-parse --verify --quiet "refs/remotes/origin/$cand^{commit}" >/dev/null 2>&1 \
      || git -C "$mdir" rev-parse --verify --quiet "refs/heads/$cand^{commit}" >/dev/null 2>&1; }; then
    printf '%s\t' "$cand"
    return 0
  fi
  lptools_aosp_die "manifest revision '$cand' matches no ref in $mdir; recording neither a branch nor a tag for it"
  printf '\t'
}

# lptools_aosp_branch <work> [default-revision] [requested-ref].
lptools_aosp_branch() {
  local pair
  pair=$(lptools_aosp_resolve_ref "$@") || return 1
  printf '%s' "${pair%%$'\t'*}"
}

# lptools_aosp_tag <work> [default-revision] [requested-ref].
lptools_aosp_tag() {
  local pair
  pair=$(lptools_aosp_resolve_ref "$@") || return 1
  printf '%s' "${pair#*$'\t'}"
}

# lptools_aosp_revision <work>: commit of the manifest repository itself.
lptools_aosp_revision() {
  local work=$1 rev=""
  [ -d "$work/.repo/manifests" ] || return 1
  rev=$(git -C "$work/.repo/manifests" rev-parse HEAD 2>/dev/null) || return 1
  [ -n "$rev" ] || return 1
  printf '%s' "$rev"
}

# lptools_aosp_synced_projects <work> <out>: write "path<TAB>name<TAB>remote
# <TAB>commit" for every manifest project that has a checkout in <work>, and
# print how many were found.
#
# "repo forall --ignore-missing" is repo's supported per-project selection: it
# visits exactly the projects that were synced and skips the rest, which
# "repo manifest -r" cannot do (it resolves every project's HEAD and dies on the
# first unchecked-out one). A project that has a checkout but no resolvable
# commit is an error, never a silent drop.
lptools_aosp_synced_projects() {
  local work=$1 out=$2 script="$2.each" errlog="$2.err" bad="" count=""
  if [ ! -d "$work/.repo" ]; then
    lptools_aosp_die "no repo metadata at $work/.repo; cannot enumerate synced projects"
    return 1
  fi
  if ! command -v repo >/dev/null 2>&1; then
    lptools_aosp_die "'repo' is not on PATH; cannot enumerate synced projects in $work"
    return 1
  fi
  rm -f -- "$out"
  cat >"$script" <<'LPTOOLS_FOREACH'
#!/bin/sh
# Runs once per synced project worktree, with repo's REPO_* environment set.
sha=$(git rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null) || sha=UNRESOLVED
printf '%s\t%s\t%s\t%s\n' "${REPO_PATH:-}" "${REPO_PROJECT:-}" "${REPO_REMOTE:-}" "$sha"
LPTOOLS_FOREACH
  chmod +x "$script" 2>/dev/null || true
  rm -f -- "$errlog"
  if ! ( cd "$work" && repo forall --ignore-missing -c sh "$script" ) >"$out" 2>"$errlog"; then
    lptools_aosp_die "repo forall failed in $work: $(tr '\n' ' ' <"$errlog" 2>/dev/null | tail -c 400)"
    return 1
  fi
  bad=$(awk -F'\t' 'NF != 4 || $1 == "" || $4 == "" || $4 == "UNRESOLVED" { print $1 ":" $4 }' "$out" | tr '\n' ' ')
  if [ -n "$bad" ]; then
    lptools_aosp_die "cannot pin a commit for synced project(s): $bad"
    return 1
  fi
  count=$(grep -c . "$out" 2>/dev/null || true)
  count=$(printf '%s' "${count:-0}" | tr -cd '0-9')
  if [ "${count:-0}" -eq 0 ]; then
    lptools_aosp_die "no synced project found in $work; refusing to record an empty pinned manifest"
    return 1
  fi
  printf '%s' "$count"
}

# lptools_write_pinned_manifest <merged-manifest> <projects.tsv> <out>: write
# only the synced projects, each pinned to the commit that is checked out,
# keeping the merged manifest's remotes, <default revision> and per-project
# attributes. Prints the number of projects written.
lptools_write_pinned_manifest() {
  local merged=$1 pins=$2 out=$3 count=""
  if [ ! -s "$merged" ]; then
    lptools_aosp_die "no merged manifest to pin ($merged)"
    return 1
  fi
  if [ ! -s "$pins" ]; then
    lptools_aosp_die "no synced project list to pin ($pins)"
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    lptools_aosp_die "'python3' is not on PATH; cannot write $out"
    return 1
  fi
  rm -f -- "$out"
  count=$(python3 - "$merged" "$pins" "$out" <<'LPTOOLS_PY'
import re
import sys
import xml.etree.ElementTree as ET

merged, pins, out = sys.argv[1:4]
sha_re = re.compile(r"^[0-9a-f]{40}$")
pinned = {}
with open(pins, encoding="utf-8", errors="replace") as fh:
    for raw in fh:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            sys.exit("pinned project line is malformed: %r" % line)
        path, _name, _remote, sha = parts
        if not path:
            sys.exit("pinned project line has no path: %r" % line)
        if not sha_re.match(sha):
            sys.exit("pinned project %s has no commit: %r" % (path, sha))
        pinned[path] = sha
if not pinned:
    sys.exit("no synced project to pin")

tree = ET.parse(merged)
root = tree.getroot()
counts = {"total": 0, "kept": 0}


def prune(parent):
    """Keep only checked-out projects, pinned to their checked-out commit."""
    for element in list(parent):
        if element.tag != "project":
            prune(element)
            continue
        counts["total"] += 1
        path = element.get("path") or element.get("name")
        if path in pinned:
            element.set("revision", pinned[path])
            counts["kept"] += 1
            prune(element)
        else:
            parent.remove(element)


prune(root)
kept_paths = {e.get("path") or e.get("name") for e in root.iter("project")}
missing = sorted(set(pinned) - kept_paths)
if missing:
    sys.exit("synced project(s) missing from the merged manifest: %s"
             % ", ".join(missing))
if counts["kept"] == 0:
    sys.exit("no synced project matched the merged manifest (%d manifest projects)"
             % counts["total"])

# An XML comment may not contain "--", so the count line is worded without it.
root.insert(0, ET.Comment(
    " lptools: partial pinned manifest: %d of %d manifest projects were "
    "synced and are pinned to their checked-out commits "
    % (counts["kept"], counts["total"])))
if hasattr(ET, "indent"):
    ET.indent(tree, space="  ")
with open(out, "w", encoding="utf-8", newline="") as fh:
    fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    fh.write(ET.tostring(root, encoding="unicode"))
    fh.write("\n")
print(counts["kept"])
LPTOOLS_PY
) || {
    lptools_aosp_die "failed to write $out"
    return 1
  }
  count=$(printf '%s' "${count:-}" | tr -cd '0-9')
  if [ "${count:-0}" -eq 0 ] || [ ! -s "$out" ] || ! grep -q '<project ' "$out" 2>/dev/null; then
    lptools_aosp_die "wrote no usable pinned manifest to $out"
    return 1
  fi
  printf '%s' "$count"
}

# _lptools_aosp_export <work> <bundle> <requested-ref> <tmpdir>: do the work and
# set AOSP_BRANCH / AOSP_TAG / AOSP_MANIFEST_REV / AOSP_PINNED_PROJECTS.
_lptools_aosp_export() {
  local work=$1 bundle=$2 requested=$3 tmp=$4
  local merged="$4/manifest.xml" pins="$4/projects.tsv" errlog="$4/repo.err"
  local defrev="" pair="" branch="" tag="" rev="" count=""

  if [ ! -d "$bundle" ]; then
    lptools_aosp_die "bundle directory $bundle does not exist"
    return 1
  fi

  lptools_aosp_merged_manifest "$work" "$merged" "$errlog" || return 1
  defrev=$(lptools_aosp_default_revision "$merged" || true)
  pair=$(lptools_aosp_resolve_ref "$work" "$defrev" "$requested") || return 1
  branch=${pair%%$'\t'*}
  tag=${pair#*$'\t'}
  # repo always names its local manifest checkout "default", whatever ref it
  # really fetched; that name must never be reported as the branch.
  if [ "$branch" = "default" ]; then
    lptools_aosp_die "manifest branch resolved to repo's synthetic local branch name 'default'; recording no branch"
    branch=""
  fi

  lptools_aosp_synced_projects "$work" "$pins" >/dev/null || return 1
  count=$(lptools_write_pinned_manifest "$merged" "$pins" "$bundle/aosp-manifest.xml") || {
    lptools_aosp_die "could not record the pinned manifest of $work"
    return 1
  }
  rev=$(lptools_aosp_revision "$work" || true)
  if [ -z "$rev" ]; then
    lptools_aosp_die "cannot resolve the manifest repository commit in $work/.repo/manifests"
    return 1
  fi

  AOSP_BRANCH=$branch
  AOSP_TAG=$tag
  AOSP_MANIFEST_REV=$rev
  AOSP_PINNED_PROJECTS=$count
  lptools_aosp_note "pinned $count synced project(s) (branch: ${branch:-none}, tag: ${tag:-none}, manifest rev: $rev)"
  return 0
}

# lptools_export_meta <work> <bundle> [requested-ref]: sets AOSP_BRANCH /
# AOSP_TAG / AOSP_MANIFEST_REV / AOSP_PINNED_PROJECTS for the caller and writes
# <bundle>/aosp-manifest.xml. Returns nonzero -- instead of reporting 0 projects
# -- when the tree cannot back up the claim, so the build stops here.
lptools_export_meta() {
  local work=${1:-} bundle=${2:-} requested=${3:-} tmp="" rc=0
  if [ -z "$work" ] || [ -z "$bundle" ]; then
    lptools_aosp_die "lptools_export_meta needs a work tree and a bundle directory"
    return 1
  fi
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/lptools-aosp-meta.XXXXXX") || {
    lptools_aosp_die "cannot create a temporary directory for the provenance export"
    return 1
  }
  _lptools_aosp_export "$work" "$bundle" "$requested" "$tmp" || rc=$?
  rm -rf -- "$tmp"
  if [ "$rc" -ne 0 ]; then
    AOSP_BRANCH=""
    AOSP_TAG=""
    AOSP_MANIFEST_REV=""
    AOSP_PINNED_PROJECTS="0"
  fi
  return "$rc"
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
