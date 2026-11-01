#!/usr/bin/env python3
"""Build manifest.json + SHA256SUMS for a lptools bundle.

The manifest is the contract between this builder and imgpatchtools-rs: it says
exactly which AOSP revision and branch the binaries came from, which project
list was used, and what every file's hash is, so a consumer can verify what it
downloaded and report a precise version.

Fields that matter for reproducibility:

  aosp_ref                what the caller asked for (tag or branch name)
  aosp_branch             the branch the manifest itself tracked
  aosp_tag                the tag at the manifest revision, when tagged
  aosp_manifest_revision  commit of platform/manifest (the metadata repo)
  aosp_pinned_manifest    {file, projects, sha256}: every project pinned
  builder                 repository / commit / branch of THIS tool
"""

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle_kind(platform):
    if platform.startswith("android-"):
        return "android"
    if platform.startswith("windows-"):
        return "windows"
    return "host"


def validate_pinned_manifest(path, expected_projects):
    """Reject empty or unpinned source records before packaging."""
    if not str(expected_projects).isdigit() or int(expected_projects) <= 0:
        raise ValueError("pinned project count must be a positive integer")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError("cannot read pinned AOSP manifest: " + str(exc)) from exc
    projects = root.findall("project")
    if root.tag != "manifest" or not projects or root.findall("include"):
        raise ValueError("pinned AOSP manifest must contain resolved projects")
    if len(projects) != int(expected_projects):
        raise ValueError("pinned AOSP project count does not match manifest")
    for project in projects:
        if not project.get("name") or not re.fullmatch(r"[0-9a-fA-F]{40}", project.get("revision", "")):
            raise ValueError("AOSP project is not pinned to a full commit: " + str(project.attrib))
    return len(projects)


def main(argv):
    ap = argparse.ArgumentParser(description="write manifest.json + SHA256SUMS")
    ap.add_argument("--dir", required=True, help="bundle directory (contains bin/)")
    ap.add_argument("--aosp-ref", required=True)
    ap.add_argument("--aosp-branch", default="")
    ap.add_argument("--aosp-tag", default="")
    ap.add_argument("--product", default="")
    ap.add_argument("--aosp-manifest-rev", default="")
    ap.add_argument("--aosp-manifest-url", default="")
    ap.add_argument("--aosp-pinned-projects", default="0")
    ap.add_argument("--builder-sha", default="")
    ap.add_argument("--builder-branch", default="")
    ap.add_argument("--builder-ref", default="")
    ap.add_argument("--builder-run", default="")
    ap.add_argument("--repo-version", default="")
    ap.add_argument("--platform", default="linux-x86_64")
    ap.add_argument("--bundle-version", default="")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.dir)
    files = {}
    tools = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in ("manifest.json", "SHA256SUMS"):
                continue
            digest = sha256(full)
            files[rel] = {"sha256": digest, "size": os.path.getsize(full)}
            if rel.startswith("bin/"):
                tools[os.path.basename(rel)] = digest

    if not tools:
        print("error: no binaries under " + os.path.join(root, "bin"), file=sys.stderr)
        return 1

    pinned_file = "aosp-manifest.xml"
    try:
        projects = validate_pinned_manifest(os.path.join(root, pinned_file),
                                           args.aosp_pinned_projects)
    except ValueError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    pinned = {
        "file": pinned_file,
        "projects": projects,
        "sha256": files[pinned_file]["sha256"],
    }

    platform = args.platform
    kind = bundle_kind(platform)
    target_abi = ""
    host_arch = ""
    if kind == "android":
        target_abi = platform.split("-", 1)[1]
    elif "-" in platform:
        host_arch = platform.split("-", 1)[1]

    manifest = {
        "schema": 2,
        "bundle": "lptools",
        "kind": kind,
        "bundle_version": args.bundle_version or args.aosp_ref,
        "platform": platform,
        "target_abi": target_abi,
        "host_arch": host_arch,
        "aosp_ref": args.aosp_ref,
        "aosp_branch": args.aosp_branch,
        "aosp_tag": args.aosp_tag,
        "aosp_manifest_revision": args.aosp_manifest_rev,
        "aosp_manifest_url": args.aosp_manifest_url,
        "aosp_pinned_manifest": pinned,
        "build_product": args.product,
        "built_at": os.environ.get("SOURCE_DATE_EPOCH_ISO", ""),
        "builder": {
            "repository": args.builder_ref,
            "commit": args.builder_sha,
            "branch": args.builder_branch,
            "run": args.builder_run,
            "repo_tool": args.repo_version,
        },
        "tools": tools,
        "files": files,
    }

    # newline="\n" keeps these files byte-identical on Windows and Linux; a
    # stray CR makes "sha256sum -c" fail with "No such file or directory".
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    with open(os.path.join(root, "SHA256SUMS"), "w", encoding="utf-8",
              newline="\n") as f:
        for rel in sorted(files) + ["manifest.json"]:
            digest = files[rel]["sha256"] if rel in files else sha256(os.path.join(root, rel))
            f.write(digest + "  " + rel + "\n")

    print("wrote %s/manifest.json (%d files, %d tools, %s)"
          % (root, len(files), len(tools), platform))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
