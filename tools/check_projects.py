#!/usr/bin/env python3
"""
check_projects.py -- validate required-projects.txt against the AOSP manifest.

Why this exists
---------------
repo sync silently accepts a project that no longer exists: a typo, or an
upstream rename, shows up 20 GB later as a missing header inside a build
failure. This script runs right after "repo init" (manifest metadata only, a
few MB) and answers one question in under a second:

    every project in required-projects.txt -> still present in this manifest?

Usage
-----
    python3 tools/check_projects.py --repo-root aosp
    python3 tools/check_projects.py --repo-root aosp --json

Exit codes: 0 = all good, 1 = missing/unknown project, 2 = manifest not found.
Only the standard library is used (Python 3.8+).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET


def norm(name):
    """AOSP project name -> checkout path, e.g. platform/build/make -> build/make."""
    return name[len("platform/"):] if name.startswith("platform/") else name


def find_manifest(repo_root):
    candidates = [
        os.path.join(repo_root, ".repo", "manifest.xml"),
        os.path.join(repo_root, ".repo", "manifests", "manifest.xml"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def manifest_base_dir(path):
    """Directory that <include name="..."> paths are relative to.

    repo resolves includes against the manifest checkout root. For a real
    checkout that is .repo/manifests (and .repo/manifest.xml is a symlink
    into it), but be tolerant of hand-made trees where it is a plain file.
    """
    d = os.path.dirname(os.path.realpath(path))
    if os.path.basename(d) == ".repo" and os.path.isdir(os.path.join(d, "manifests")):
        return os.path.join(d, "manifests")
    return d


def load_manifest(path):
    """Return ({project_name: {paths...}}, [warnings])."""
    projects = {}
    warnings = []
    seen_files = set()
    base_dir = manifest_base_dir(path)

    def parse_file(xml_path, depth=0):
        real = os.path.realpath(xml_path)
        if real in seen_files:
            return
        seen_files.add(real)
        if depth > 8:
            warnings.append("include depth limit hit at " + xml_path)
            return
        try:
            root = ET.parse(real).getroot()
        except ET.ParseError as exc:
            warnings.append("cannot parse " + xml_path + ": " + str(exc))
            return
        except OSError as exc:
            warnings.append("cannot read " + xml_path + ": " + str(exc))
            return

        for node in root:
            tag = node.tag
            if tag == "include":
                name = node.get("name")
                if not name:
                    continue
                # repo resolves includes against the manifest root; fall back
                # to the including file's directory for hand-made trees.
                inc = os.path.join(base_dir, name)
                if not os.path.exists(inc):
                    alt = os.path.join(os.path.dirname(real), name)
                    if os.path.exists(alt):
                        inc = alt
                parse_file(inc, depth + 1)
            elif tag == "project":
                name = node.get("name")
                if not name:
                    continue
                p = node.get("path") or norm(name)
                projects.setdefault(name, set()).add(p)
            elif tag == "remove-project":
                name = node.get("name")
                if name:
                    projects.pop(name, None)

    parse_file(path)
    return projects, warnings


def read_required(list_path):
    out = []
    with open(list_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def main(argv):
    ap = argparse.ArgumentParser(description="Validate AOSP project list")
    ap.add_argument("--repo-root", default="aosp",
                    help="directory that contains .repo/ (default: aosp)")
    ap.add_argument("--list", default="required-projects.txt",
                    help="project list file (default: required-projects.txt)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    manifest = find_manifest(args.repo_root)
    if not manifest:
        print("error: no repo manifest under " + args.repo_root +
              "/.repo -- run repo init first", file=sys.stderr)
        return 2

    projects, warnings = load_manifest(manifest)
    known_names = set(projects)
    known_paths = set()
    for paths in projects.values():
        known_paths.update(paths)

    required = read_required(args.list)
    missing = []
    resolved = []
    for req in required:
        expected_path = norm(req)
        if req in known_names:
            resolved.append({"project": req, "path": sorted(projects[req])[0]})
        elif expected_path in known_paths or req in known_paths:
            resolved.append({"project": req, "path": expected_path})
        else:
            missing.append(req)

    if args.json:
        print(json.dumps({
            "manifest": manifest,
            "manifest_projects": len(projects),
            "required": len(required),
            "missing": missing,
            "resolved": resolved,
            "warnings": warnings,
        }, indent=2))
    else:
        print("manifest : " + manifest)
        print("projects : %d in manifest, %d required" % (len(projects), len(required)))
        for w in warnings:
            print("warning  : " + w)
        if missing:
            print()
            print("MISSING from this manifest (fix required-projects.txt, or pin a"
                  " different -b ref):")
            for m in missing:
                print("  - " + m)
        else:
            print("OK: every required project resolves in this manifest.")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
