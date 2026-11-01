#!/usr/bin/env python3
"""Executable LP (super.img) compatibility matrix: old writers, latest readers.

Every image is produced by a *real* lpmake named by the caller; nothing here
re-implements the LP or sparse format:

    lp-compatibility.py --latest-bin /srv/android-latest-release/bin \\
        --writer aosp10=/srv/aosp10/bin/lpmake --out compat-out

--latest-bin needs lpmake, lpdump, lpunpack and simg2img (img2simg is used only
when a writer lacks --sparse). Readers always come from --latest-bin, so there is
no --reader-bin switch; writer tags come only from TAG=PATH, never from a file
name. Per writer: raw v10.0 (writer lpmake -> latest lpdump + lpunpack --slot=0/1,
then a latest-lpmake repack -> same checks); sparse v10.0 (writer --sparse when
advertised, else latest img2simg over the writer's raw payload; readers try the
sparse container directly and fall back to simg2img as a recorded front end,
never asserted as direct support); v10.2 virtual A/B only when the writer's
--help advertises it (absent = unsupported capability, not a failure; both kinds
of skip are logged as such). Slot-suffixed partitions (_a/_b) are resolved, so a
virtual A/B image is checked against the same inputs. Optional zero-length and
non-4K partition probes skip when the writer rejects them.
Every extracted partition is sha256-compared with the exact input bytes.

Each case records argv/rc/stdout/stderr (full text in <out>/logs), duration,
sha256 and tool provenance; reports go to <out>/lp-compatibility.json and .md.
Exit 1 when a required case fails; unsupported capabilities and optional probe
failures are reported separately and do not change the exit code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MIB = 1024 * 1024
SECTOR = 512
META_SIZE = 65536
META_SLOTS = 2
DEVICE_SIZE = 32 * MIB
GROUP_NAME = "dyn"
GROUP_SIZE = 22 * MIB
MAIN_PARTS = (("system", 1052672), ("vendor", 2 * MIB))
ZERO_PART = ("empty", 0)
ODD_PART = ("oddtail", 1049600)  # 512-aligned, deliberately not 4K-aligned
LATEST_TOOLS = ("lpmake", "lpdump", "lpunpack", "simg2img")


class HarnessError(Exception):
    pass


def _dec(data):
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def _tail(text, limit=300):
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "writer"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_bytes(size, seed):
    """Deterministic, non-trivial input bytes (no RNG, no /dev/urandom)."""
    out, state = bytearray(), hashlib.sha256(seed.encode("utf-8")).digest()
    while len(out) < size:
        state = hashlib.sha256(state).digest()
        out += state
    return bytes(out[:size])


def run(cmd, timeout):
    argv, started = [str(part) for part in cmd], time.monotonic()
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
        rc, out, err = proc.returncode, _dec(proc.stdout), _dec(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        rc, out, err = 124, _dec(exc.stdout), _dec(exc.stderr) + "\n[timeout: no exit in %ss]" % timeout
    except OSError as exc:
        rc, out, err = 127, "", "cannot execute %s: %s" % (argv[0], exc)
    return {"argv": argv, "rc": rc, "stdout": out, "stderr": err,
            "ms": int((time.monotonic() - started) * 1000)}


def probe_flags(exe, timeout):
    """(advertised --flags, first help line) exactly as the tool prints them."""
    flags, first = set(), ""
    for argv in ([exe, "--help"], [exe, "-h"]):
        res = run(argv, timeout)
        text = res["stdout"] + "\n" + res["stderr"]
        flags |= set(re.findall(r"--[a-z0-9][a-z0-9-]*", text))
        first = first or next((line.strip() for line in text.splitlines() if line.strip()), "")
        if flags:
            break
    return flags, first


def parse_writer_arg(text):
    tag, sep, path = text.partition("=")
    if not sep or not tag.strip() or not path.strip():
        raise HarnessError("--writer expects TAG=PATH, got %r" % text)
    exe = Path(path.strip()).expanduser()
    if not exe.is_file():
        raise HarnessError("--writer %s: no such file: %s" % (tag.strip(), exe))
    if not os.access(exe, os.X_OK):
        raise HarnessError("--writer %s: not executable: %s" % (tag.strip(), exe))
    return {"tag": tag.strip(), "path": str(exe.resolve()), "sha256": sha256_file(exe),
            "flags": [], "help_first_line": "", "inputs": {}}


def resolve_latest(bin_dir):
    root = Path(bin_dir).expanduser()
    if not root.is_dir():
        raise HarnessError("--latest-bin is not a directory: %s" % root)
    tools = {}
    for name in LATEST_TOOLS + ("img2simg",):
        found = next((p for p in (root / name, root / (name + ".exe")) if p.is_file()), None)
        tools[name] = {"path": str(found.resolve()), "sha256": sha256_file(found)} if found else None
    missing = [name for name in LATEST_TOOLS if tools[name] is None]
    if missing:
        raise HarnessError("--latest-bin %s is missing: %s" % (root, ", ".join(missing)))
    return {"bin_dir": str(root.resolve()),
            "tools": {name: tool for name, tool in tools.items() if tool is not None}}


class Case:
    def __init__(self, case_id, writer, tier, container):
        self.id, self.writer, self.tier, self.container = case_id, writer, tier, container
        self.commands, self.checks, self.notes = [], [], []
        self.status, self.skip_reason, self.skip_kind, self.failure = None, "", "", ""
        self.metadata_version, self.geometry = None, None

    def check(self, name, ok, expected=None, actual=None, detail=""):
        self.checks.append({"name": name, "ok": bool(ok), "expected": expected, "actual": actual,
                            "detail": detail})
        return bool(ok)

    def skip(self, reason, kind="unsupported"):
        self.status, self.skip_reason, self.skip_kind = "skip", reason, kind
        return self

    def finish(self):
        bad = [c for c in self.checks if not c["ok"]]
        if bad:
            self.status = "fail"
            self.failure = "; ".join("%s: expected %r, got %r"
                                     % (c["name"], c["expected"], c["actual"]) for c in bad[:3])
        elif self.status is None:
            self.status = "pass" if self.checks else "skip"
            if not self.checks:
                self.skip_reason, self.skip_kind = self.skip_reason or "no checks ran", "blocked"
        return self

    def to_json(self):
        return {"id": self.id, "writer": self.writer, "tier": self.tier, "container": self.container,
                "status": self.status, "skip_reason": self.skip_reason, "skip_kind": self.skip_kind,
                "failure": self.failure, "metadata_version": self.metadata_version,
                "geometry": self.geometry, "notes": self.notes, "checks": self.checks,
                "commands": [dict(c, stdout=_tail(c["stdout"], 2000), stderr=_tail(c["stderr"], 2000),
                                  stdout_bytes=len(c["stdout"]), stderr_bytes=len(c["stderr"]))
                             for c in self.commands]}


class Ctx:
    def __init__(self, latest, out, timeout):
        self.latest, self.out, self.timeout, self.latest_flags = latest["tools"], out, timeout, set()
        (out / "logs").mkdir(parents=True, exist_ok=True)

    def run(self, case, label, cmd):
        res = run(cmd, self.timeout)
        res["label"] = label
        case.commands.append(res)
        log = self.out / "logs" / (re.sub(r"[^A-Za-z0-9._-]+", "_", "%s__%02d__%s"
                                          % (case.id, len(case.commands), label)) + ".log")
        log.write_text("$ %s\n[rc %s in %sms]\n--- stdout ---\n%s\n--- stderr ---\n%s\n"
                       % (" ".join(shlex.quote(a) for a in res["argv"]), res["rc"], res["ms"],
                          res["stdout"], res["stderr"]), encoding="utf-8", errors="replace")
        res["log"] = str(log.relative_to(self.out))
        return res


EXT_RE = re.compile(r"^\s*(\d+)\s*\.\.\s*(\d+)\s+linear\s+(\S+)\s+(\d+)\s*$")
# Sizes come from "Super partition layout:" lines, which spell out "(N sectors)";
# the extent range above is printed by different lpdump versions in ways that
# are ambiguous for the partition size, so it is only used to count extents.
LAYOUT_RE = re.compile(r"^\s*\S+:\s*\d+\s*\.\.\s*\d+:\s*(\S+)\s+\((\d+) sectors\)\s*$")


def _resolve_part(info, name):
    """Partition name as lpdump shows it: virtual A/B adds an _a suffix."""
    for candidate in (name, name + "_a"):
        if candidate in info["partitions"]:
            return candidate
    return None


def _group_size(groups, name):
    """Maximum size of the named group (virtual A/B groups are suffixed _a)."""
    for candidate in (name, name + "_a"):
        if candidate in groups:
            return groups[candidate]
    return None


def _chunk(text, header, stops):
    start = text.find(header)
    if start < 0:
        return ""
    start += len(header)
    ends = [end for end in (text.find(stop, start) for stop in stops) if end >= 0]
    return text[start:min(ends)] if ends else text[start:]


def parse_dump(text):
    """Parse lpdump text for version + geometry (never exact-text equality)."""
    info = {"version": None, "metadata_size": None, "metadata_used": None, "slots": None, "device_size": None,
            "partitions": {}, "groups": {}}
    for key, pattern in (("version", r"Metadata version:\s*(\d+)\.(\d+)"),
                         ("metadata_used", r"Metadata size:\s*(\d+)"),
                         ("metadata_size", r"Metadata max size:\s*(\d+)"),
                         ("slots", r"Metadata slot count:\s*(\d+)")):
        match = re.search(pattern, text)
        if match:
            info[key] = ".".join(match.groups()) if key == "version" else int(match.group(1))
    current = None
    for line in _chunk(text, "Partition table:",
                       ("Super partition layout:", "Block device table:", "Group table:")).splitlines():
        match = re.match(r"^\s*Name:\s*(\S+)\s*$", line)
        if match:
            current = match.group(1)
            info["partitions"][current] = {"group": None, "size": 0, "extents": 0}
            continue
        match = re.match(r"^\s*Group:\s*(\S+)\s*$", line)
        if match and current:
            info["partitions"][current]["group"] = match.group(1)
            continue
        if EXT_RE.match(line) and current:
            info["partitions"][current]["extents"] += 1
    for line in text.splitlines():
        match = LAYOUT_RE.match(line)
        if match and match.group(1) in info["partitions"]:
            info["partitions"][match.group(1)]["size"] = int(match.group(2)) * SECTOR
    match = re.search(r"^\s*Size:\s*(\d+)\s+bytes",
                      _chunk(text, "Block device table:", ("Group table:",)), re.MULTILINE)
    info["device_size"] = int(match.group(1)) if match else None
    current = None
    for line in _chunk(text, "Group table:", ()).splitlines():
        match = re.match(r"^\s*Name:\s*(\S+)\s*$", line)
        if match:
            current = match.group(1)
            info["groups"][current] = None
            continue
        match = re.match(r"^\s*Maximum size:\s*(\d+)", line)
        if match and current:
            info["groups"][current] = int(match.group(1))
    return info


def lpmake_argv(exe, output, parts, images, sparse=False, virtual_ab=False):
    argv = [str(exe), "--device-size", str(DEVICE_SIZE), "--metadata-size", str(META_SIZE),
            "--metadata-slots", str(META_SLOTS), "--super-name", "super",
            "--group", "%s:%d" % (GROUP_NAME, GROUP_SIZE)]
    for name, size in parts:
        argv += ["--partition", "%s:readonly:%d:%s" % (name, size, GROUP_NAME)]
        if name in images:
            argv += ["--image", "%s=%s" % (name, images[name]["path"])]
    if virtual_ab:
        argv.append("--virtual-ab")
    if sparse:
        argv.append("--sparse")
    return argv + ["--output", str(output)]


def build_super(ctx, case, exe, img, parts, images, label="lpmake", sparse=False, virtual_ab=False):
    res = ctx.run(case, label, lpmake_argv(exe, img, parts, images, sparse=sparse, virtual_ab=virtual_ab))
    ok = case.check(label + " exit 0", res["rc"] == 0, 0, res["rc"], _tail(res["stderr"]))
    size = img.stat().st_size if img.is_file() else "missing"
    return case.check(label + " wrote an image", size != "missing" and size > 0, "> 0 bytes", size) and ok


def run_ok(ctx, case, label, cmd):
    res = ctx.run(case, label, cmd)
    return case.check(label + " exit 0", res["rc"] == 0, 0, res["rc"], _tail(res["stderr"]))


def fresh_directory(path):
    """Keep previous evidence, but never consume it as output from this attempt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=path.name + " ", dir=path.parent))


def ensure_raw(ctx, case, img, label, storage):
    storage = fresh_directory(storage.with_suffix("")) / storage.name
    if not run_ok(ctx, case, label, [ctx.latest["simg2img"]["path"], str(img), str(storage)]):
        return None
    written = storage.is_file() and storage.stat().st_size > 0
    return storage if case.check(label + " wrote a raw image", written, "> 0 bytes",
                                 storage.stat().st_size if storage.is_file() else "missing") else None


def unpack_slot(ctx, case, img, slot, parts, images, label, outdir, sparse, fallback):
    outdir = fresh_directory(outdir)
    exe = ctx.latest["lpunpack"]["path"]
    res = ctx.run(case, label + ":lpunpack", [exe, "--slot=%d" % slot, str(img), str(outdir)])
    mode = "direct sparse input" if sparse else "raw input"
    if res["rc"] != 0 and sparse:
        raw = fallback or ensure_raw(ctx, case, img, label + ":desparse",
                                     outdir.with_name(outdir.name + " desparse.img"))
        if raw is not None:
            outdir = fresh_directory(outdir.with_name(outdir.name + " desparse"))
            res = ctx.run(case, label + ":lpunpack-desparse",
                          [exe, "--slot=%d" % slot, str(raw), str(outdir)])
            mode = "simg2img front end"
    if res["rc"] != 0:
        return {"ok": False, "mode": mode, "problems": ["lpunpack rc=%d: %s" % (res["rc"], _tail(res["stderr"]))]}
    problems, hashes, extracted = [], {}, {}
    for name, size in parts:
        found = outdir / (name + ".img")
        if not found.exists() and (outdir / (name + "_a.img")).exists():
            found = outdir / (name + "_a.img")
        if size == 0:
            if found.exists() and found.stat().st_size != 0:
                problems.append("%s.img is %d bytes, expected empty" % (name, found.stat().st_size))
            hashes[name] = None
        elif not found.exists():
            problems.append(name + ".img missing")
            continue
        else:
            hashes[name] = sha256_file(found)
            extracted[name] = {"path": str(found), "size": found.stat().st_size, "sha256": hashes[name]}
            if hashes[name] != images[name]["sha256"]:
                problems.append("%s.img sha256 %s != input %s"
                                % (name, hashes[name], images[name]["sha256"]))
    return {"ok": not problems, "mode": mode, "problems": problems, "hashes": hashes,
            "images": extracted}


def verify_super(ctx, case, img, parts, images, label, minor, scratch, sparse=False, require_slot1=True):
    exe, fallback, mode = ctx.latest["lpdump"]["path"], None, "direct sparse input" if sparse else "raw input"
    dump = ctx.run(case, label + ":lpdump", [exe, str(img)])
    if dump["rc"] != 0 and sparse:
        fallback = ensure_raw(ctx, case, img, label + ":desparse",
                              scratch.with_name(scratch.name + " raw view.img"))
        if fallback is not None:
            dump = ctx.run(case, label + ":lpdump-desparse", [exe, str(fallback)])
            mode = "simg2img front end"
    if sparse:
        case.notes.append("%s: lpdump read the sparse container via %s" % (label, mode))
    if not case.check(label + ": lpdump exit 0", dump["rc"] == 0, 0, dump["rc"], _tail(dump["stderr"])):
        return None
    info = parse_dump(dump["stdout"] + "\n" + dump["stderr"])
    case.metadata_version = info["version"]
    case.geometry = {key: info[key] for key in ("metadata_size", "slots", "device_size", "partitions", "groups")}
    names = sorted(name for name, _ in parts)
    resolved = {name: _resolve_part(info, name) for name, _ in parts}
    allowed = {name for name in resolved.values() if name}
    if minor == 2:
        for name, _ in parts:
            inactive = info["partitions"].get(name + "_b")
            if (resolved[name] == name + "_a" and inactive is not None
                    and inactive["size"] == 0 and inactive["extents"] == 0):
                allowed.add(name + "_b")
    sizes = {name: info["partitions"].get(resolved[name] or name, {}).get("size")
             for name, _ in parts}
    for name, ok, expected, actual in (
            ("metadata version", info["version"] == "10.%d" % minor, "10.%d" % minor, info["version"]),
            ("geometry", (info["metadata_size"], info["slots"], info["device_size"])
             == (META_SIZE, META_SLOTS, DEVICE_SIZE), (META_SIZE, META_SLOTS, DEVICE_SIZE),
             (info["metadata_size"], info["slots"], info["device_size"])),
            ("group %s max size" % GROUP_NAME, _group_size(info["groups"], GROUP_NAME) == GROUP_SIZE,
             GROUP_SIZE, _group_size(info["groups"], GROUP_NAME)),
            ("partition set", all(resolved.values()) and set(info["partitions"]) <= allowed
             and (minor == 2 or all(resolved[name] == name for name, _ in parts)),
             names, sorted(info["partitions"])),
            ("partition sizes", all(sizes[name] == size for name, size in parts),
             dict(parts), sizes)):
        case.check("%s: %s" % (label, name), ok, expected, actual)
    info["extracted_images"] = {}
    for slot in (0, 1):
        res = unpack_slot(ctx, case, img, slot, parts, images, "%s slot%d" % (label, slot),
                          scratch / ("slot%d" % slot), sparse, fallback)
        if slot == 0 and res["ok"]:
            info["extracted_images"] = res["images"]
        if slot == 1 and not require_slot1:
            case.notes.append("%s slot1: %s" % (label, "extracted byte-exact" if res["ok"] else
                                                "not required for this image (%s)" % "; ".join(res["problems"])))
        else:
            case.check("%s slot%d extraction byte-exact" % (label, slot), res["ok"],
                       "every partition sha256 == input", "; ".join(res["problems"]) or "ok")
        if sparse and slot == 0:
            case.notes.append("%s slot0: sparse container handled via %s" % (label, res["mode"]))
    return info


def verify_repack(ctx, case, img, parts, images, minor, sparse=False, virtual_ab=False, require_slot1=True):
    """Rebuild the same partitions with the latest lpmake, then verify byte-exactly."""
    latest = ctx.latest
    if sparse and "--sparse" not in ctx.latest_flags:
        if "img2simg" not in latest:
            case.skip("unsupported: latest lpmake has no --sparse and no img2simg for the repack")
            return None
        raw = img.with_name(img.stem + " raw.img")
        if not build_super(ctx, case, latest["lpmake"]["path"], raw, parts, images, "latest lpmake repack"):
            return None
        case.notes.append("latest lpmake has no --sparse: repack wrapped by latest img2simg")
        if not run_ok(ctx, case, "img2simg repack", [latest["img2simg"]["path"], str(raw), str(img)]):
            return None
    else:
        label = "latest lpmake repack" + (" --sparse" if sparse else "") + (" --virtual-ab" if virtual_ab else "")
        if not build_super(ctx, case, latest["lpmake"]["path"], img, parts, images, label,
                           sparse=sparse, virtual_ab=virtual_ab):
            return None
    return verify_super(ctx, case, img, parts, images, "repack", minor, img.with_suffix(".scratch"),
                        sparse=sparse, require_slot1=require_slot1)


def run_writer(ctx, writer, wd):
    tag, slug = writer["tag"], _slug(writer["tag"])
    indir = wd / "input images"
    indir.mkdir(parents=True, exist_ok=True)
    images = {}
    for name, size in MAIN_PARTS:
        path = indir / (name + " input.img")
        path.write_bytes(make_bytes(size, "%s:%s" % (tag, name)))
        images[name] = {"path": str(path), "size": size, "sha256": sha256_file(path)}
    writer["inputs"] = images
    latest, cases = ctx.latest, []

    case = Case(slug + "/raw-v10.0", tag, "required", "raw")
    raw = wd / "super raw.img"
    if build_super(ctx, case, writer["path"], raw, MAIN_PARTS, images):
        case.check("raw image is the device size", raw.stat().st_size == DEVICE_SIZE,
                   DEVICE_SIZE, raw.stat().st_size)
        info = verify_super(ctx, case, raw, MAIN_PARTS, images, "image", 0, wd / "verify raw")
        if info and info["extracted_images"]:
            verify_repack(ctx, case, wd / "repack raw.img", MAIN_PARTS, info["extracted_images"], 0)
    case.finish()
    cases.append(case)
    raw_built = bool(raw.is_file() and raw.stat().st_size)

    case = Case(slug + "/sparse-v10.0", tag, "required", "sparse")
    img = wd / "super sparse.img"
    if "--sparse" in writer["flags"]:
        built = build_super(ctx, case, writer["path"], img, MAIN_PARTS, images, "lpmake --sparse", sparse=True)
    elif raw_built and "img2simg" in latest:
        built = run_ok(ctx, case, "img2simg", [latest["img2simg"]["path"], str(raw), str(img)])
        case.notes.append("writer advertises no --sparse: the writer's raw payload was wrapped by latest "
                          "img2simg, so this fixture tests sparse reading, not the writer's sparse output")
    else:
        built = False
        case.skip("unsupported: writer has no --sparse and --latest-bin has no img2simg")
    if built:
        info = verify_super(ctx, case, img, MAIN_PARTS, images, "image", 0, wd / "verify sparse", sparse=True)
        if info and info["extracted_images"]:
            verify_repack(ctx, case, wd / "repack sparse.img", MAIN_PARTS, info["extracted_images"], 0, sparse=True)
    case.finish()
    cases.append(case)

    case = Case(slug + "/virtual-ab-v10.2", tag, "required", "raw")
    if "--virtual-ab" not in writer["flags"]:
        case.tier = "optional"
        case.skip("unsupported: writer lpmake does not advertise --virtual-ab (capability absent, not a failure)")
    else:
        img = wd / "super virtual-ab.img"
        res = ctx.run(case, "lpmake --virtual-ab",
                      lpmake_argv(writer["path"], img, MAIN_PARTS, images, virtual_ab=True))
        if res["rc"] in (124, 127):  # could not run the writer: a failure, not a capability gap
            case.check("lpmake --virtual-ab exit 0", False, 0, res["rc"], _tail(res["stderr"]))
        elif res["rc"] != 0:
            case.check("lpmake advertised --virtual-ab exit 0", False, 0, res["rc"], _tail(res["stderr"]))
        elif not (img.is_file() and img.stat().st_size > 0):
            case.check("lpmake --virtual-ab wrote an image", False, "> 0 bytes",
                       img.stat().st_size if img.is_file() else "missing")
        else:
            case.check("lpmake --virtual-ab wrote an image", True, "> 0 bytes", img.stat().st_size)
            info = verify_super(ctx, case, img, MAIN_PARTS, images, "image", 2, wd / "verify vab",
                                require_slot1=False)
            can_repack = case.check("latest lpmake supports virtual A/B repack",
                                    "--virtual-ab" in ctx.latest_flags, "--virtual-ab", sorted(ctx.latest_flags))
            if can_repack and info and info["extracted_images"]:
                verify_repack(ctx, case, wd / "repack vab.img", MAIN_PARTS, info["extracted_images"], 2,
                              virtual_ab=True, require_slot1=False)
    case.finish()
    cases.append(case)

    case = Case(slug + "/zero-length-partition", tag, "optional", "raw")
    parts = MAIN_PARTS + (ZERO_PART,)
    img = wd / "super zero-length.img"
    if not raw_built:
        case.skip("blocked: this writer produced no raw v10.0 image to extend")
    elif build_super(ctx, case, writer["path"], img, parts, images):
        verify_super(ctx, case, img, parts, images, "image", 0, wd / "verify zero")
    case.finish()
    cases.append(case)

    case = Case(slug + "/non-4k-partition", tag, "optional", "raw")
    odd = indir / "oddtail input.img"
    odd.write_bytes(make_bytes(ODD_PART[1], tag + ":oddtail"))
    odd_images = dict(images, oddtail={"path": str(odd), "size": ODD_PART[1], "sha256": sha256_file(odd)})
    parts = MAIN_PARTS + (ODD_PART,)
    img = wd / "super non-4k.img"
    res = ctx.run(case, "lpmake", lpmake_argv(writer["path"], img, parts, odd_images))
    if res["rc"] in (124, 127):  # could not run: a failure, not a capability gap
        case.check("lpmake exit 0", False, 0, res["rc"], _tail(res["stderr"]))
    elif res["rc"] != 0:
        case.skip("unsupported: writer lpmake rejected a non-4K partition size (rc=%d): %s"
                  % (res["rc"], _tail(res["stderr"], 200)))
    else:
        case.check("lpmake exit 0", True, 0, 0)
        verify_super(ctx, case, img, parts, odd_images, "image", 0, wd / "verify odd")
    case.finish()
    cases.append(case)
    return cases


def summarize(cases):
    summary = {"total": len(cases), "passed": 0, "failed_required": 0, "failed_optional": 0,
               "skipped": 0, "skipped_unsupported": 0, "skipped_blocked": 0,
               "failed": [], "unsupported": [], "writers": {}}
    for case in cases:
        summary["writers"].setdefault(case.writer, {"pass": 0, "fail": 0, "skip": 0})[case.status] += 1
        if case.status == "pass":
            summary["passed"] += 1
        elif case.status == "fail":
            summary["failed_required" if case.tier == "required" else "failed_optional"] += 1
            summary["failed"].append({"id": case.id, "tier": case.tier, "failure": case.failure})
        else:
            summary["skipped"] += 1
            summary["skipped_unsupported" if case.skip_kind == "unsupported" else "skipped_blocked"] += 1
            summary["unsupported"].append({"id": case.id, "tier": case.tier, "reason": case.skip_reason,
                                           "kind": case.skip_kind})
    return summary


def render_markdown(report):
    bt, summary = chr(96), report["summary"]
    lines = ["# LP tools compatibility matrix", "", "Generated: %s  " % report["generated_at"],
             "Latest bundle: %s%s%s  " % (bt, report["latest"]["bin_dir"], bt), "",
             "## Verdict", "", report["verdict"]["headline"], "",
             "| case | tier | status | metadata | note |", "| --- | --- | --- | --- | --- |"]
    for case in report["cases"]:
        note = case["skip_reason"] if case["status"] == "skip" else (case["failure"] or "")
        lines.append("| %s%s%s | %s | **%s** | %s | %s |" % (
            bt, case["id"], bt, case["tier"], case["status"].upper(), case["metadata_version"] or "-",
            note.replace("|", "/")[:160]))
    lines += ["", "## Provenance", "", "| writer | executable | sha256 | flags |",
              "| --- | --- | --- | --- |"]
    lines += ["| %s | %s%s%s | %s%s%s | %s |" % (w["tag"], bt, w["path"], bt, bt, w["sha256"], bt,
                                                 " ".join(w["flags"]) or "-") for w in report["writers"]]
    lines += ["- latest %s%s%s sha256 %s%s%s" % (bt, name, bt, bt, tool["sha256"], bt)
              for name, tool in sorted(report["latest"]["tools"].items())]
    lines += ["", "Fixtures: device %d bytes, metadata %d bytes x %d slots, group %s:%d."
              % (DEVICE_SIZE, META_SIZE, META_SLOTS, GROUP_NAME, GROUP_SIZE), ""]
    for title, items in (("Unsupported capabilities", summary["unsupported"]),
                         ("Failures", summary["failed"])):
        lines += ["## " + title, ""] + ["- %s%s%s (%s): %s" % (
            bt, item["id"], bt, item["tier"], item.get("reason") or item.get("failure")) for item in items]
        if not items:
            lines.append("None.")
        lines.append("")
    lines += ["## Required coverage gaps", ""]
    lines += ["- " + error for error in report["coverage_errors"]] or ["None."]
    lines += ["", "## Failed case commands", ""]
    failed = {item["id"] for item in summary["failed"]}
    for case in report["cases"]:
        if case["id"] in failed:
            lines += ["- %s%s%s: %s%s%s rc=%s" % (bt, case["id"], bt, bt, " ".join(
                shlex.quote(part) for part in command["argv"]), bt, command["rc"]) for command in case["commands"]]
    if not failed:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def required_coverage(writers, cases):
    """All advertised required scenarios must exist exactly once and pass."""
    errors = []
    if not writers:
        errors.append("no historical writers were tested")
    for writer in writers:
        slug = _slug(writer["tag"])
        scenarios = ["raw-v10.0", "sparse-v10.0"]
        if "--virtual-ab" in writer["flags"]:
            scenarios.append("virtual-ab-v10.2")
        for scenario in scenarios:
            case_id = slug + "/" + scenario
            matches = [case for case in cases if case.id == case_id and case.writer == writer["tag"]]
            if len(matches) != 1 or matches[0].tier != "required" or matches[0].status != "pass":
                errors.append(case_id + ": expected exactly one passing required case")
    for case in cases:
        if case.tier == "required" and case.status != "pass":
            errors.append(case.id + ": required case " + str(case.status))
    return errors


def build_report(latest, writers, cases):
    summary = summarize(cases)
    coverage_errors = required_coverage(writers, cases)
    failed = summary["failed_required"]
    incompatible = failed > 0 or bool(coverage_errors)
    headline = ("FAIL: %d required case(s) failed; required coverage %s." %
                (failed, "incomplete" if coverage_errors else "complete")) if incompatible else (
        "PASS: latest readers handled every required fixture from %d writer(s); %d unsupported "
        "capabilit%s recorded separately." % (len(writers), summary["skipped_unsupported"],
                                             "y" if summary["skipped_unsupported"] == 1 else "ies"))
    return {"schema": "lp-compatibility/1", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": {"python": platform.python_version(), "system": platform.system(),
                     "machine": platform.machine()},
            "harness": {"path": str(Path(__file__).resolve()),
                        "sha256": sha256_file(Path(__file__).resolve())},
            "geometry": {"device_size": DEVICE_SIZE, "metadata_size": META_SIZE,
                         "metadata_slots": META_SLOTS, "group": GROUP_NAME, "group_size": GROUP_SIZE,
                         "partitions": [{"name": name, "size": size} for name, size in MAIN_PARTS]},
            "latest": latest, "writers": writers, "cases": [case.to_json() for case in cases],
            "summary": summary,
            "coverage_complete": not coverage_errors, "coverage_errors": coverage_errors,
            "verdict": {"exit_code": 1 if incompatible else 0, "compatible": not incompatible,
                        "headline": headline}}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build super images with real historical lpmake executables, then check the latest tools.")
    parser.add_argument("--latest-bin", required=True, metavar="DIR",
                        help="directory with the latest lpmake/lpdump/lpunpack/simg2img")
    parser.add_argument("--writer", action="append", default=[], metavar="TAG=PATH",
                        help="repeatable: TAG=PATH of an actual historical lpmake executable")
    parser.add_argument("--out", required=True, metavar="DIR", help="output directory for report and logs")
    parser.add_argument("--timeout", type=int, default=600, metavar="SECONDS",
                        help="per-command timeout (default 600)")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error("--out must be an empty directory; use a fresh path to keep prior evidence")
    out.mkdir(parents=True, exist_ok=True)
    try:
        if not args.writer:
            raise HarnessError("at least one --writer TAG=PATH is required")
        latest = resolve_latest(args.latest_bin)
        writers = [parse_writer_arg(spec) for spec in args.writer]
        if len({writer["tag"] for writer in writers}) != len(writers):
            raise HarnessError("duplicate --writer tag in: %s" % ", ".join(w["tag"] for w in writers))
    except HarnessError as exc:
        print("lp-compatibility: %s" % exc, file=sys.stderr)
        return 2

    ctx = Ctx(latest, out, args.timeout)
    ctx.latest_flags, _ = probe_flags(latest["tools"]["lpmake"]["path"], args.timeout)
    for writer in writers:
        flags, first_line = probe_flags(writer["path"], args.timeout)
        writer["flags"], writer["help_first_line"] = sorted(flags), first_line
    print("latest bundle: %s" % latest["bin_dir"])
    cases = []
    for writer in writers:
        print("== writer %s: %s (sha256 %s)" % (writer["tag"], writer["path"], writer["sha256"]))
        print("   advertised: %s" % (" ".join(writer["flags"]) or "none"))
        writer_cases = run_writer(ctx, writer, out / "work dir" / ("writer " + _slug(writer["tag"])))
        for case in writer_cases:
            print("   [%-4s] %-34s %s" % (case.status.upper(), case.id,
                                           case.skip_reason if case.status == "skip" else case.failure))
        cases.extend(writer_cases)
    report = build_report(latest, writers, cases)
    (out / "lp-compatibility.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "lp-compatibility.md").write_text(render_markdown(report), encoding="utf-8")
    summary = report["summary"]
    print("\n" + report["verdict"]["headline"])
    print("cases: %d passed, %d required failed, %d optional failed, %d skipped (%d unsupported)"
          % (summary["passed"], summary["failed_required"], summary["failed_optional"],
             summary["skipped"], summary["skipped_unsupported"]))
    print("report: %s" % (out / "lp-compatibility.json"))
    print("report: %s" % (out / "lp-compatibility.md"))
    return report["verdict"]["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
