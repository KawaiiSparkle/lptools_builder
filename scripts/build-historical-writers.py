#!/usr/bin/env python3
"""Build one lpmake per historical AOSP release ref, from unmodified AOSP source.

Why: the LP (logical partition) metadata format changed across Android 10..16.
A compatibility matrix needs a *real* writer for each metadata version, not a
reimplementation. This script compiles the production liblp sources from an
exact historical commit together with that ref's lpmake.cc, using the latest
AOSP support libraries (libbase, liblog, libsparse, libext4_utils, libz and
libc++) and the latest clang/libc++ headers. Nothing in the historical sources
is patched: if old source does not compile against the new support headers, the
ref is reported as failed with its compiler log and the matrix keeps going.

    build-historical-writers.py \
        --aosp /srv/android-latest-release \
        --out /srv/historical \
        --latest-bin /srv/android-latest-release/out/soong/host/linux-x86/bin

Outputs:
    <out>/<tag>/bin/lpmake          one executable per ref (fixture writer only)
    <out>/<tag>/manifest.json       commits, per-file sha256, exact commands+logs
    <out>/writer-list.json          every requested ref with status/path, for
                                    tests/lp-compatibility.py --writer TAG=PATH
    <out>/source/<tag>/...          cached raw Gitiles blobs (hashed, reused)

Constraints that shape this: Linux-only (AOSP host toolchain, ELF, rpath);
android.googlesource.com must be reachable over TLS (verified, retried, no git);
sources are fetched by exact commit so a moving branch cannot change a build.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import platform
import re
import shlex
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

GITILES = "https://android.googlesource.com"
CORE_REPO = "platform/system/core"
EXTRAS_REPO = "platform/system/extras"
LIBLP_DIR = "fs_mgr/liblp"
LPMAKE_SRC = "partition_tools/lpmake.cc"
DEFAULT_REFS = ["android-%d.0.0_r1" % v for v in range(10, 17)]
TEST_SKIP_DIRS = {"tests", "test", "TESTING"}
UA = "lptools-historical-writers/1 (+https://android.googlesource.com)"
SSL_CONTEXT = ssl.create_default_context()
SUPPORT_LIBS = ("libc++.so", "libc++abi.so", "libunwind.so", "libbase.so", "liblog.so",
                "libcrypto_utils.so", "libcrypto.so", "libext4_utils.so", "libsparse.so",
                "libz.so")
REQUIRED_LIBS = ("libbase.so", "liblog.so", "libsparse.so", "libc++.so")
SUPPORT_HEADERS = (("libbase", ("system/core/libbase/include", "system/libbase/include")),
                   ("liblog", ("system/logging/liblog/include", "system/core/liblog/include")),
                   ("libcutils", ("system/core/libcutils/include",)),
                   ("libext4_utils", ("system/extras/ext4_utils/include",)),
                   ("libsparse", ("system/core/libsparse/include",)),
                   ("boringssl", ("external/boringssl/src/include",)))


class BuildError(Exception):
    """A ref could not be fetched, resolved or compiled; reported, not fatal."""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def tag_for(ref):
    """aosp10..aosp16 for the r1 releases; aosp14_r7 style for other releases."""
    match = re.fullmatch(r"android-(\d+)(?:\.\d+)*_r(\d+)", ref)
    if match:
        major, release = match.group(1), match.group(2)
        return "aosp" + major if release == "1" else "aosp%s_r%s" % (major, release)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-") or "ref"


def http_get(url, timeout=60, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410):
                raise BuildError("%s: HTTP %d" % (url, exc.code)) from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(1.0 * (attempt + 1))
    raise BuildError("%s: %s" % (url, last))


def decode_gitiles_json(payload):
    """Gitiles JSON is JSON behind the standard XSSI guard line (")]}'")."""
    text = payload.decode("utf-8", "replace")
    if text.startswith(")]}'"):
        text = text[4:]
    return json.loads(text)


def decode_gitiles_blob(payload):
    """Gitiles ?format=TEXT is base64 (possibly line-wrapped) file content."""
    return base64.b64decode(payload.strip())


def gitiles_json(repo, rev, path=""):
    target = ("%s/%s" % (rev, path)) if path else rev
    url = "%s/%s/+/%s?format=JSON" % (GITILES, repo, target)
    try:
        return url, decode_gitiles_json(http_get(url))
    except ValueError as exc:
        raise BuildError("%s: invalid JSON (%s)" % (url, exc)) from exc


def gitiles_blob(repo, commit, path):
    url = "%s/%s/+/%s/%s?format=TEXT" % (GITILES, repo, commit, path)
    try:
        return url, decode_gitiles_blob(http_get(url))
    except (ValueError, binascii.Error) as exc:
        raise BuildError("%s: invalid base64 (%s)" % (url, exc)) from exc


def resolve_commit(repo, ref):
    url, data = gitiles_json(repo, ref)
    commit = data.get("commit") or data.get("id")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BuildError("%s: no commit in ref metadata" % url)
    return {"repo": repo, "ref": ref, "commit": commit, "url": url}


def is_test_path(path):
    parts = path.split("/")
    return any(part in TEST_SKIP_DIRS for part in parts) or path.endswith("_test.cpp")


def fetch_tree(store, repo, commit, subdir):
    """Recursively fetch a small source tree through Gitiles JSON at a pinned commit."""
    slug = repo.replace("/", "_")
    fetched, stack, seen = [], [subdir], set()
    while stack:
        base = stack.pop()
        if base in seen or base.count("/") > subdir.count("/") + 8:
            continue
        seen.add(base)
        url, data = gitiles_json(repo, commit, base)
        for entry in data.get("entries", []):
            path = base + "/" + entry["name"]
            if entry.get("type") == "tree":
                if entry["name"] not in TEST_SKIP_DIRS:
                    stack.append(path)
            elif entry.get("type") == "blob":
                if not is_test_path(path):
                    fetched.append((path, entry["id"]))
    files = []
    for path, blob_id in sorted(fetched):
        target = store / slug / path
        raw_url, payload = gitiles_blob(repo, commit, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or sha256_file(target) != sha256_bytes(payload):
            target.write_bytes(payload)
        files.append({"repo": repo, "commit": commit, "path": path,
                      "gitiles_url": raw_url, "sha256": sha256_bytes(payload),
                      "size": len(payload), "baked_in_path": str(target)})
    return files


def tokenize_bp(text):
    tokens, i, size = [], 0, len(text)
    while i < size:
        char = text[i]
        if char in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = size if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = size if end < 0 else end + 2
        elif char == '"':
            end = i + 1
            while end < size and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            tokens.append(text[i:end + 1])
            i = end + 1
        elif char in "{}[]:,+=":
            tokens.append(char)
            i += 1
        else:
            match = re.match(r"(?:[A-Za-z_][A-Za-z0-9_]*|-?[0-9]+)", text[i:])
            if not match:
                raise BuildError("Android.bp: cannot tokenize at %r" % text[i:i + 20])
            tokens.append(match.group(0))
            i += len(match.group(0))
    return tokens


def _bp_value(tokens, i, variables=None):
    variables = variables or {}
    token = tokens[i]
    if token == "[":
        value, i = [], i + 1
        while tokens[i] != "]":
            item, i = _bp_value(tokens, i, variables)
            value.append(item)
            if tokens[i] == ",":
                i += 1
        i += 1
    elif token == "{":
        value, i = _bp_dict(tokens, i + 1, variables)
    elif token.startswith('"'):
        value, i = json.loads(token), i + 1
    else:
        value, i = variables.get(token, ("ident", token)), i + 1
    while i < len(tokens) and tokens[i] == "+":
        extra, i = _bp_value(tokens, i + 1, variables)
        if isinstance(value, list) and isinstance(extra, list):
            value = value + extra
        elif isinstance(value, str) and isinstance(extra, str):
            value = value + extra
        else:
            raise BuildError("Android.bp: unsupported concatenation")
    return value, i


def _bp_dict(tokens, i, variables=None):
    result = {}
    while tokens[i] != "}":
        key = tokens[i]
        i += 1
        if tokens[i] != ":":
            raise BuildError("Android.bp: expected colon after %s" % key)
        value, i = _bp_value(tokens, i + 1, variables)
        if key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] += value
        else:
            result[key] = value
        if tokens[i] == ",":
            i += 1
    return result, i + 1


def parse_bp(text):
    tokens = tokenize_bp(text)
    blocks, variables, i = [], {}, 0
    while i < len(tokens):
        kind = tokens[i]
        if i + 1 < len(tokens) and tokens[i + 1] == "=":
            variables[kind], i = _bp_value(tokens, i + 2, variables)
            continue
        if tokens[i + 1:i + 3] == ["+", "="]:
            value, i = _bp_value(tokens, i + 3, variables)
            if kind not in variables or not isinstance(value, list):
                raise BuildError("Android.bp: invalid variable append: %s" % kind)
            variables[kind] = variables[kind] + value
            continue
        if i + 1 >= len(tokens) or tokens[i + 1] != "{":
            raise BuildError("Android.bp: expected module block after %r" % kind)
        block, i = _bp_dict(tokens, i + 2, variables)
        block["__kind__"] = kind
        blocks.append(block)
    return blocks



def _as_list(value):
    return value if isinstance(value, list) else []


def _strings(value):
    return [item for item in _as_list(value) if isinstance(item, str)]


def find_module(blocks, name):
    return next((b for b in blocks if b.get("name") == name), None)


def select_sources(blocks):
    """Production C++ sources of the liblp defaults block, host-relevant only."""
    module = find_module(blocks, "liblp_defaults")
    if module is None:
        candidates = [b for b in blocks if b.get("name") == "liblp"
                      and str(b.get("__kind__", "")).startswith("cc_library")]
        if not candidates:
            raise BuildError("Android.bp: no liblp_defaults and no host liblp module")
        base, host, excluded = [], {}, []
        for block in candidates:
            base += _strings(block.get("srcs"))
            target = block.get("target") if isinstance(block.get("target"), dict) else {}
            host = target.get("host") if isinstance(target.get("host"), dict) else {}
            base += _strings(host.get("srcs"))
            excluded += _strings(block.get("exclude_srcs")) + _strings(host.get("exclude_srcs"))
        module = {"srcs": base, "exclude_srcs": excluded}
    target = module.get("target") if isinstance(module.get("target"), dict) else {}
    host = target.get("host") if isinstance(target.get("host"), dict) else {}
    srcs = _strings(module.get("srcs")) + _strings(host.get("srcs"))
    drop = set(_strings(module.get("exclude_srcs"))) | set(_strings(host.get("exclude_srcs")))
    keep = []
    for src in srcs:
        if src in drop or is_test_path(src) or not src.endswith((".cpp", ".cc")):
            continue
        if src not in keep:
            keep.append(src)
    if not keep:
        raise BuildError("Android.bp: liblp defaults declare no production sources")
    includes = _strings(module.get("export_include_dirs")) or ["include"]
    return sorted(keep), includes


def parse_global_go(text):
    """Extract Soong's configured default clang and C++ standard."""
    info = {"clang_version": None, "clang_short_version": None, "cpp_std": None}
    for key, pattern in (("clang_version", r"ClangDefaultVersion\s*=\s*\"([^\"]+)\""),
                         ("clang_short_version", r"ClangDefaultShortVersion\s*=\s*\"([^\"]+)\"")):
        match = re.search(pattern, text)
        if match:
            info[key] = match.group(1)
    match = re.search(r"[A-Za-z_]*[Cc]pp[Ss]td[A-Za-z_]*\s*=\s*\"([^\"]*\+\+[^\"]*)\"", text)
    if match:
        info["cpp_std"] = match.group(1)
    return info


def resolve_toolchain(aosp):
    go_path = aosp / "build/soong/cc/config/global.go"
    info = {"clang_version": None, "clang_short_version": None, "cpp_std": None}
    source = {"path": None, "sha256": None, "matched": {}}
    if go_path.is_file():
        text = go_path.read_text(encoding="utf-8", errors="replace")
        info = parse_global_go(text)
        source = {"path": str(go_path), "sha256": sha256_file(go_path),
                  "matched": {k: v for k, v in info.items() if v}}
    root = aosp / "prebuilts/clang/host/linux-x86"
    installed = sorted(root.glob("clang-r*"), key=lambda p: [int(t) for t in re.findall(r"\d+", p.name)] or [0]) if root.is_dir() else []
    chosen, fallback = None, False
    if info["clang_version"]:
        candidate = root / info["clang_version"]
        if (candidate / "bin/clang++").is_file():
            chosen = candidate
    if chosen is None:
        for candidate in reversed(installed):
            if (candidate / "bin/clang++").is_file():
                chosen, fallback = candidate, bool(info["clang_version"])
                break
    if chosen is None:
        raise BuildError("no clang++ under %s (expected Soong's configured prebuilt)" % root)
    headers = None
    for probe in [chosen / "include/c++/v1"] + sorted(chosen.glob("lib64/clang/*/include/c++/v1")) \
            + sorted(chosen.glob("lib/clang/*/include/c++/v1")):
        if probe.is_dir():
            headers = probe
            break
    if headers is None:
        raise BuildError("no libc++ headers in %s (searched include/c++/v1)" % chosen)
    return {"clang++": str(chosen / "bin/clang++"), "clang_dir": str(chosen),
            "clang_version": chosen.name, "configured": info, "fallback": fallback,
            "global_go": source, "libcxx_include": str(headers),
            "cpp_std": info["cpp_std"] or "gnu++17"}


def support_headers(aosp):
    found, missing = [], []
    for name, candidates in SUPPORT_HEADERS:
        path = next((aosp / rel for rel in candidates if (aosp / rel).is_dir()), None)
        if path is None:
            missing.append(name)
        else:
            found.append({"name": name, "path": str(path)})
    if "libbase" in missing:
        raise BuildError("latest libbase headers not found under %s (needed to compile "
                         "historical liblp)" % aosp)
    return found, missing


def support_libs(lib_dir):
    found = []
    for name in SUPPORT_LIBS:
        path = lib_dir / name
        if path.is_file():
            found.append({"name": name, "path": str(path), "sha256": sha256_file(path)})
    names = {item["name"] for item in found}
    absent = [name for name in REQUIRED_LIBS if name not in names]
    if absent:
        raise BuildError("%s lacks %s; point --latest-bin at a bundle whose sibling "
                         "lib64 has the support libraries" % (lib_dir, ", ".join(absent)))
    return found


def latest_tool_hashes(latest_bin):
    tools = {}
    for name in ("lpmake", "lpdump", "lpunpack", "simg2img"):
        path = latest_bin / name
        if path.is_file():
            tools[name] = {"path": str(path), "sha256": sha256_file(path)}
    return tools


def bundle_manifest(latest_bin):
    for candidate in (latest_bin.parent / "manifest.json", latest_bin / "manifest.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except ValueError:
                continue
            return {"path": str(candidate), "sha256": sha256_file(candidate),
                    "aosp_ref": data.get("aosp_ref"), "aosp_manifest_revision":
                    data.get("aosp_manifest_revision"), "bundle_version": data.get("bundle_version")}
    return None


class Runner:
    def __init__(self, tag, logs):
        self.tag, self.dir, self.records = tag, logs, []
        self.dir.mkdir(parents=True, exist_ok=True)

    def run(self, label, argv, cwd=None, timeout=900):
        started = time.monotonic()
        try:
            proc = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout,
                                  env={**os.environ, "LC_ALL": "C"})
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err = 124, exc.stdout or b"", (exc.stderr or b"") + b"\n[timeout]"
        except OSError as exc:
            raise BuildError("cannot execute %s: %s" % (argv[0], exc)) from exc
        log = self.dir / ("%02d-%s.log" % (len(self.records) + 1,
                                           re.sub(r"[^A-Za-z0-9._-]+", "_", label)))
        log.write_text("$ %s\n[rc %d in %d ms]\n--- stdout ---\n%s\n--- stderr ---\n%s\n"
                       % (" ".join(shlex.quote(str(a)) for a in argv), rc,
                          int((time.monotonic() - started) * 1000),
                          out.decode("utf-8", "replace"), err.decode("utf-8", "replace")),
                       encoding="utf-8", errors="replace")
        record = {"label": label, "argv": [str(a) for a in argv], "rc": rc,
                  "ms": int((time.monotonic() - started) * 1000), "log": str(log),
                  "stdout_tail": out.decode("utf-8", "replace")[-2000:],
                  "stderr_tail": err.decode("utf-8", "replace")[-4000:]}
        self.records.append(record)
        return record


def compile_flags(toolchain, source_root, include_dirs, support_inc):
    """Flags for one translation unit: historical liblp headers win over support ones."""
    dirs = [str(source_root / LIBLP_DIR / "include"), str(source_root / LIBLP_DIR)]
    dirs += [str(source_root / LIBLP_DIR / item) for item in include_dirs]
    dirs += [item["path"] for item in support_inc]
    flags = ["-std=%s" % toolchain["cpp_std"], "-O2", "-g0", "-D_FILE_OFFSET_BITS=64",
             "-fno-strict-aliasing", "-nostdinc++"]
    seen = set()
    for path in dirs:
        if str(path) not in seen:
            seen.add(str(path))
            flags += ["-I", str(path)]
    return flags + ["-isystem", toolchain["libcxx_include"]]


def compile_and_link(tag_out, source_root, srcs, include_dirs, toolchain, support_inc,
                     support_lib_dir, libs, runner, lpmake_src):
    obj_dir = tag_out / "obj"
    obj_dir.mkdir(parents=True, exist_ok=True)
    clangpp = toolchain["clang++"]
    common = compile_flags(toolchain, source_root, include_dirs, support_inc)
    manifest_flags = list(common)
    compile_commands, objects = [], []
    for src in srcs:
        path = source_root / LIBLP_DIR / src
        if not path.is_file():
            raise BuildError("Android.bp lists %s but the pinned commit has no such file" % src)
        obj = obj_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", src) + ".o")
        record = runner.run("compile-" + src.replace("/", "_"),
                            [clangpp, "-c", str(path), "-o", str(obj)] + common)
        compile_commands.append(record)
        if record["rc"] != 0:
            raise BuildError("%s failed to compile (rc=%d): %s"
                             % (src, record["rc"], record["stderr_tail"].strip()[-600:]))
        objects.append(str(obj))
    lpmake_obj = obj_dir / "lpmake.o"
    record = runner.run("compile-lpmake",
                        [clangpp, "-c", str(lpmake_src), "-o", str(lpmake_obj)] + common)
    compile_commands.append(record)
    if record["rc"] != 0:
        raise BuildError("lpmake.cc failed to compile (rc=%d): %s"
                         % (record["rc"], record["stderr_tail"].strip()[-600:]))
    objects.append(str(lpmake_obj))
    binary = tag_out / "bin" / "lpmake"
    binary.parent.mkdir(parents=True, exist_ok=True)
    link_argv = [clangpp, "-o", str(binary)] + objects + ["-nostdlib++", "-pthread", "-lm",
                 "-Wl,-rpath," + str(support_lib_dir),
                 "-Wl,-rpath-link," + str(support_lib_dir)] + [item["path"] for item in libs]
    record = runner.run("link-lpmake", link_argv)
    if record["rc"] != 0:
        raise BuildError("link failed (rc=%d): %s" % (record["rc"],
                                                      record["stderr_tail"].strip()[-600:]))
    return binary, compile_commands, record, manifest_flags


def needed_libraries(binary, runner):
    try:
        record = runner.run("readelf-needed", ["readelf", "-d", str(binary)])
    except BuildError:
        return []
    if record["rc"] != 0:
        return []
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", record["stdout_tail"])


def build_ref(aosp, out, ref, tag, latest_bin, lib_dir, timeout):
    tag_out = out / tag
    store = out / "source" / tag
    runner = Runner(tag, out / "logs" / tag)
    manifest = {"schema": 1, "kind": "historical-writer", "tag": tag, "ref": ref,
                "status": "failed", "binary": None, "failure": None,
                "commits": {}, "liblp": {}, "lpmake": None, "sources": [],
                "toolchain": {},
                "build": {"platform": "linux-%s" % platform.machine(), "compile_commands": [],
                          "compile_flags": [], "link": None, "timeout_s": timeout},
                "logs": [],
                "support": {"lib_dir": str(lib_dir), "rpath": str(lib_dir),
                            "latest_bin": str(latest_bin), "headers": [], "libs": [],
                            "bundle_manifest": bundle_manifest(latest_bin),
                            "latest_tools": latest_tool_hashes(latest_bin)},
                "disclosure": {
                    "historical_sources": "liblp production sources and lpmake.cc are "
                    "unmodified AOSP files at the pinned commits below",
                    "substituted_support": "libbase, liblog, libcutils headers, libsparse, "
                    "libext4_utils, libz, libc++ headers and libc++.so are the latest "
                    "release tree, not the historical ref",
                    "never": "no source patching, no format reimplementation, no writer "
                    "substitution; a ref that does not compile stays failed"}}
    try:
        if not sys.platform.startswith("linux"):
            raise BuildError("historical writers are Linux-only (ELF/rpath/AOSP host clang); "
                             "host is %s" % sys.platform)
        store.mkdir(parents=True, exist_ok=True)
        core = resolve_commit(CORE_REPO, ref)
        extras = resolve_commit(EXTRAS_REPO, ref)
        manifest["commits"] = {"platform/system/core": core, "platform/system/extras": extras}
        write_json(store / "pin.json", {"tag": tag, "ref": ref, "commits": manifest["commits"]})
        sources = fetch_tree(store, CORE_REPO, core["commit"], LIBLP_DIR)
        extra_files = []
        for path in (LPMAKE_SRC, "partition_tools/Android.bp"):
            url, payload = gitiles_blob(EXTRAS_REPO, extras["commit"], path)
            target = store / EXTRAS_REPO.replace("/", "_") / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            extra_files.append({"repo": EXTRAS_REPO, "commit": extras["commit"],
                                "path": path, "gitiles_url": url,
                                "sha256": sha256_bytes(payload), "size": len(payload),
                                "baked_in_path": str(target)})
        lpmake_file = next((item for item in extra_files
                            if item["path"] == LPMAKE_SRC), None)
        if lpmake_file is None:
            raise BuildError("pinned %s has no %s" % (extras["commit"], LPMAKE_SRC))
        sources += [lpmake_file]
        manifest["sources"] = sources
        bp_path = store / CORE_REPO.replace("/", "_") / LIBLP_DIR / "Android.bp"
        if not bp_path.is_file():
            raise BuildError("pinned liblp tree has no Android.bp")
        bp_text = bp_path.read_text(encoding="utf-8", errors="replace")
        srcs, include_dirs = select_sources(parse_bp(bp_text))
        manifest["liblp"] = {"dir": LIBLP_DIR, "srcs": srcs, "include_dirs": include_dirs,
                             "android_bp": {"path": str(bp_path),
                                            "sha256": sha256_file(bp_path),
                                            "size": bp_path.stat().st_size}}
        manifest["lpmake"] = {"path": LPMAKE_SRC, "sha256": lpmake_file["sha256"],
                              "size": lpmake_file["size"]}
        toolchain = resolve_toolchain(aosp)
        manifest["toolchain"] = toolchain
        headers, missing = support_headers(aosp)
        libs = support_libs(lib_dir)
        manifest["support"]["headers"] = headers
        manifest["support"]["missing_headers"] = missing
        manifest["support"]["libs"] = libs
        manifest["build"]["compiler"] = toolchain
        manifest["build"]["cpp_std"] = toolchain["cpp_std"]
        version = runner.run("clang-version", [toolchain["clang++"], "--version"])
        manifest["build"]["compiler_version"] = version["stdout_tail"].splitlines()[:3]
        binary, commands, link, flags = compile_and_link(
            tag_out, store / CORE_REPO.replace("/", "_"), srcs, include_dirs, toolchain,
            headers, lib_dir, libs, runner, store / EXTRAS_REPO.replace("/", "_") / LPMAKE_SRC)
        manifest["build"]["compile_commands"] = commands
        manifest["build"]["compile_flags"] = flags
        manifest["build"]["link"] = link
        manifest["binary"] = {"path": str(binary), "sha256": sha256_file(binary),
                              "size": binary.stat().st_size,
                              "needed": needed_libraries(binary, runner)}
        manifest["status"] = "built"
    except (BuildError, OSError) as exc:
        # Keep every command that ran before the failure, including the failing one.
        manifest["failure"] = str(exc)
        manifest["build"]["compile_commands"] = list(runner.records)
        manifest["logs"] = [item["log"] for item in runner.records]
    manifest["logs"] = [item["log"] for item in runner.records]
    write_json(tag_out / "manifest.json", manifest)
    return {"tag": tag, "ref": ref, "status": manifest["status"],
            "path": manifest["binary"]["path"] if manifest["binary"] else None,
            "sha256": manifest["binary"]["sha256"] if manifest["binary"] else None,
            "writer_arg": "%s=%s" % (tag, manifest["binary"]["path"]) if manifest["binary"] else None,
            "commits": {key: value.get("commit") for key, value in manifest["commits"].items()},
            "error": manifest["failure"], "log_dir": str(runner.dir)}


def write_writer_list(out, refs, results, latest_bin, lib_dir):
    """writer-list.json is the matrix input: one entry per requested ref, always."""
    built = sum(1 for item in results if item["status"] == "built")
    payload = {
        "schema": 1,
        "created_with": "scripts/build-historical-writers.py",
        "out": str(out), "latest_bin": str(latest_bin), "lib_dir": str(lib_dir),
        "requested_refs": refs, "tags": [item["tag"] for item in results],
        "summary": {"built": built, "failed": len(results) - built, "total": len(results)},
        "writers": results,
        "note": "historical writers exist only to generate LP fixtures; they are not "
                "standalone release artifacts and expect the support lib64 via rpath"}
    write_json(out / "writer-list.json", payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="compile historical AOSP lpmake writers (unmodified liblp + lpmake.cc)",
        epilog="Linux only. Requires a built latest AOSP tree for clang, libc++ headers and "
               "the support shared libraries in out/soong/host/linux-x86/lib64.")
    parser.add_argument("--aosp", required=True,
                        help="latest AOSP checkout (build/soong, prebuilts, system/core, ...)")
    parser.add_argument("--out", required=True, help="output root for per-tag writers")
    parser.add_argument("--ref", action="append", default=None,
                        help="AOSP ref to build (repeatable; default android-10..16 r1)")
    parser.add_argument("--latest-bin", default=None,
                        help="latest lpmake/lpdump/lpunpack bin dir (default: <aosp>/out/soong/"
                             "host/linux-x86/bin)")
    parser.add_argument("--timeout", type=int, default=900, help="per-command timeout seconds")
    args = parser.parse_args(argv)

    aosp = Path(args.aosp).resolve()
    out = Path(args.out).resolve()
    latest_bin = Path(args.latest_bin).resolve() if args.latest_bin else \
        aosp / "out/soong/host/linux-x86/bin"
    lib_dir = latest_bin.parent / "lib64"
    if not lib_dir.is_dir():
        lib_dir = aosp / "out/soong/host/linux-x86/lib64"
    refs = args.ref or DEFAULT_REFS
    tags, seen = [], set()
    for ref in refs:
        tag = tag_for(ref)
        while tag in seen:
            tag += "_2"
        seen.add(tag)
        tags.append(tag)
    if not sys.platform.startswith("linux"):
        parser.error("this builder is Linux-only; run it on the CI Linux runner")
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for ref, tag in zip(refs, tags):
        print("[*] %s -> %s" % (ref, tag), file=sys.stderr)
        try:
            result = build_ref(aosp, out, ref, tag, latest_bin, lib_dir, args.timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced, never silently replaced
            traceback.print_exc()
            print("[!] %s aborted: %s" % (tag, exc), file=sys.stderr)
            result = {"tag": tag, "ref": ref, "status": "failed", "path": None,
                      "sha256": None, "writer_arg": None, "commits": {},
                      "error": "unhandled %s: %s" % (type(exc).__name__, exc),
                      "log_dir": str(out / "logs" / tag)}
        results.append(result)
        print("[%s] %s %s" % ("ok" if result["status"] == "built" else "FAIL",
                              result["tag"], result["path"] or result["error"]), file=sys.stderr)
    payload = write_writer_list(out, refs, results, latest_bin, lib_dir)
    print("%d/%d historical writers built; list: %s"
          % (payload["summary"]["built"], payload["summary"]["total"], out / "writer-list.json"))
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())