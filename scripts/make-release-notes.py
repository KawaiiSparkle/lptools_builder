#!/usr/bin/env python3
"""Turn the downloaded bundles into release notes.

Reads every <dist>/*/manifest.json plus the matching *.sha256 files and writes
Markdown that answers, without opening anything:

  - which AOSP commit and branch the artifacts were built from,
  - which commit of this builder produced them,
  - what each bundle contains and what its checksum is.
"""

import argparse
import glob
import hashlib
import json
import os
import sys

BACKTICK = "\x60"


def code(text):
    return BACKTICK + text + BACKTICK


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    ap = argparse.ArgumentParser(description="write RELEASE_NOTES.md")
    ap.add_argument("--dist", required=True)
    ap.add_argument("--tag", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--out", default="RELEASE_NOTES.md")
    args = ap.parse_args(argv)

    manifests = []
    for path in sorted(glob.glob(os.path.join(args.dist, "*", "manifest.json"))):
        try:
            m = load(path)
        except (OSError, ValueError) as exc:
            print("warning: skipping %s (%s)" % (path, exc), file=sys.stderr)
            continue
        m["_bundle"] = os.path.basename(os.path.dirname(path))
        manifests.append(m)

    if not manifests:
        print("error: no manifest.json under " + args.dist, file=sys.stderr)
        return 1

    first = manifests[0]
    branch = first.get("aosp_branch") or first.get("aosp_ref") or "unknown"
    tag = first.get("aosp_tag") or ""
    ref = first.get("aosp_ref", "")
    rev = first.get("aosp_manifest_revision") or ""
    pinned = first.get("aosp_pinned_manifest") or {}
    builder = first.get("builder") or {}
    repo = args.repo or builder.get("repository", "") or ""
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo[len("git@github.com:"):]
    if repo.endswith(".git"):
        repo = repo[:-4]

    lines = []
    add = lines.append

    add("## AOSP source")
    add("")
    add("| field | value |")
    add("| --- | --- |")
    add("| ref requested | " + (ref or "unknown") + " |")
    add("| branch fetched | " + (branch or "unknown") + " |")
    if tag:
        add("| tag | " + tag + " |")
    add("| manifest commit | " + (rev or "unknown") + " |")
    if rev:
        add("| manifest | https://android.googlesource.com/platform/manifest/+/" + rev + " |")
    if pinned.get("sha256"):
        add("| pinned projects | %s (pinned manifest sha256 %s) |"
            % (pinned.get("projects", 0), code(pinned["sha256"][:16] + "...")))
    add("")

    if builder:
        add("## Builder")
        add("")
        add("| field | value |")
        add("| --- | --- |")
        if repo:
            add("| repository | " + repo + " |")
        add("| commit | " + str(builder.get("commit") or "unknown") + " |")
        if builder.get("branch"):
            add("| branch | " + builder["branch"] + " |")
        if builder.get("run"):
            add("| run | " + builder["run"] + " |")
        if args.run_url:
            add("| logs | " + args.run_url + " |")
        add("")

    add("## Bundles")
    add("")
    add("| platform | kind | tools | sha256 |")
    add("| --- | --- | --- | --- |")
    for m in sorted(manifests, key=lambda x: x.get("platform", "")):
        name = m.get("_bundle", "")
        suffix = ".zip" if m.get("kind") == "windows" else ".tar.gz"
        asset = name + suffix
        sum_path = os.path.join(args.dist, asset + ".sha256")
        digest = ""
        if os.path.exists(sum_path):
            with open(sum_path, "r", encoding="utf-8") as f:
                digest = f.read().split()[0]
        else:
            archive = os.path.join(args.dist, asset)
            if os.path.exists(archive):
                h = hashlib.sha256()
                with open(archive, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                digest = h.hexdigest()
        tools = ", ".join(sorted(m.get("tools", {})))
        add("| %s | %s | %s | %s |"
            % (m.get("platform", name), m.get("kind", ""), tools or "-",
               code(digest) if digest else "-"))
    add("")

    add("## Verify")
    add("")
    add("1. Download the bundle for your platform and check its checksum")
    add("   (Linux: sha256sum -c <asset>.sha256, Windows: certutil -hashfile <asset> SHA256).")
    add("2. Extract it and read manifest.json: it pins every file's SHA-256 plus the")
    add("   AOSP branch, tag and manifest commit listed above.")
    add("3. Point imgpatchtools-rs at the extracted directory (LPTOOLS_DIR=<dir> or")
    add("   --lptools-dir) and run: imgpatchtools super doctor")
    add("")

    if any(m.get("kind") == "android" for m in manifests):
        add("Android bundles are device binaries (bionic). Push the bin/ directory to")
        add("the device and run with LD_LIBRARY_PATH pointing at the bundle's lib/.")
        add("")

    if any(m.get("kind") == "windows" for m in manifests):
        add("Windows bundles use .zip for convenient extraction; lpdump.exe is")
        add("built from the lpdumps target - same source, fully static, no DLL chain.")
        add("")

    text = "\n".join(lines).rstrip() + "\n"
    if args.tag:
        text = "lptools " + args.tag + "\n\n" + text
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("wrote " + args.out + " (%d bundles)" % len(manifests))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
