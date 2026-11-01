#!/usr/bin/env python3
"""Unit tests for tests/lp-compatibility.py.

These tests only exercise the harness itself - argument parsing, deterministic
inputs, lpdump text parsing, argv grammar, report shaping and the exit-code rule.
They never claim that any real tool combination is compatible: that requires an
actual run with real lpmake/lpdump/lpunpack binaries.  The file name contains a
dash, so the module is loaded through importlib.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("lp_compatibility", HERE / "lp-compatibility.py")
lp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lp)

V10_DUMP = """\
Metadata version: 10.0
Metadata size: 512
Metadata max size: 65536
Metadata slot count: 2
Partition table:
------------------------
  Name: system
  Group: dyn
  Attributes: readonly
  Extents:
    0 .. 2055 linear super 2048

  Name: vendor
  Group: dyn
  Attributes: readonly
  Extents:
    0 .. 4095 linear super 4103

Super partition layout:
------------------------
super: 2048 .. 6143: system (2055 sectors)
super: 4103 .. 8198: vendor (4096 sectors)

Block device table:
------------------------
  Partition name: super
  Size: 33554432 bytes

Group table:
------------------------
  Name: dyn
  Maximum size: 23068672
"""

V10_2_DUMP = V10_DUMP.replace("Metadata version: 10.0", "Metadata version: 10.2")


def empty_dump():
    return ("Metadata version: 10.0\nMetadata size: 512\nMetadata max size: 65536\nMetadata slot count: 2\n"
            "Partition table:\n------------------------\n\nBlock device table:\n"
            "------------------------\n  Partition name: super\n  Size: 33554432 bytes\n\n"
            "Group table:\n------------------------\n  Name: dyn\n  Maximum size: 23068672\n")


def write_script(path, body):
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lp compat test "))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class InputTests(TmpDirTest):
    def test_make_bytes_is_deterministic_and_sized(self):
        first = lp.make_bytes(4096, "aosp10:system")
        self.assertEqual(first, lp.make_bytes(4096, "aosp10:system"))
        self.assertEqual(len(first), 4096)
        self.assertNotEqual(first, b"\0" * 4096)
        self.assertNotEqual(first, lp.make_bytes(4096, "aosp10:vendor"))

    def test_main_parts_are_at_least_one_mib_and_sector_aligned(self):
        for name, size in lp.MAIN_PARTS:
            self.assertGreaterEqual(size, 1024 * 1024, name)
            self.assertEqual(size % lp.SECTOR, 0, name)
        for _name, size in (lp.ZERO_PART, lp.ODD_PART):
            self.assertEqual(size % lp.SECTOR, 0)
        self.assertTrue(lp.ODD_PART[1] % 4096)  # deliberately not 4K aligned

    def test_sha256_file_matches_hashlib(self):
        import hashlib
        path = self.tmp / "payload with spaces.img"
        payload = lp.make_bytes(2048, "seed")
        path.write_bytes(payload)
        self.assertEqual(lp.sha256_file(path), hashlib.sha256(payload).hexdigest())


class ArgvTests(TmpDirTest):
    def test_lpmake_argv_grammar(self):
        images = {"system": {"path": "/tmp/in put/system input.img",
                             "sha256": "0" * 64, "size": 1052672}}
        argv = lp.lpmake_argv("/bin/lpmake", "/tmp/out dir/super raw.img",
                              lp.MAIN_PARTS, images, sparse=True, virtual_ab=True)
        self.assertIn("--image", argv)
        self.assertEqual(argv[argv.index("--image") + 1], "system=/tmp/in put/system input.img")
        partition = argv[argv.index("--partition") + 1]
        self.assertEqual(partition, "system:readonly:1052672:dyn")
        self.assertEqual(argv[argv.index("--device-size") + 1], str(lp.DEVICE_SIZE))
        self.assertEqual(argv[argv.index("--metadata-size") + 1], "65536")
        self.assertEqual(argv[argv.index("--metadata-slots") + 1], "2")
        self.assertEqual(argv[argv.index("--group") + 1], "dyn:%d" % lp.GROUP_SIZE)
        self.assertLess(argv.index("--sparse"), argv.index("--output"))
        self.assertEqual(argv[-2:], ["--output", "/tmp/out dir/super raw.img"])

    def test_lpmake_argv_omits_optional_flags(self):
        argv = lp.lpmake_argv("/bin/lpmake", "out.img", lp.MAIN_PARTS, {})
        self.assertNotIn("--sparse", argv)
        self.assertNotIn("--virtual-ab", argv)
        self.assertNotIn("--image", argv)
        self.assertEqual(argv[-1], "out.img")

    def test_parse_writer_arg_requires_tag_path(self):
        for bad in ("noequals", "=path", "tag=", ""):
            with self.assertRaises(lp.HarnessError):
                lp.parse_writer_arg(bad)
        with self.assertRaises(lp.HarnessError):
            lp.parse_writer_arg("aosp10=/nonexistent/lpmake")

    def test_parse_writer_arg_reads_only_what_the_user_said(self):
        exe = write_script(self.tmp / "lpmake", "exit 0\n")
        writer = lp.parse_writer_arg("aosp-10=" + exe)
        self.assertEqual(writer["tag"], "aosp-10")
        self.assertEqual(writer["path"], str(Path(exe).resolve()))
        self.assertEqual(len(writer["sha256"]), 64)
        self.assertEqual(writer["flags"], [])

    def test_resolve_latest_names_missing_tools(self):
        with self.assertRaises(lp.HarnessError) as caught:
            lp.resolve_latest(self.tmp / "nope")
        self.assertIn("not a directory", str(caught.exception))
        with self.assertRaises(lp.HarnessError) as caught:
            lp.resolve_latest(self.tmp)
        for name in lp.LATEST_TOOLS:
            self.assertIn(name, str(caught.exception))


class ParseDumpTests(unittest.TestCase):
    def test_reads_version_and_geometry(self):
        info = lp.parse_dump(V10_DUMP)
        self.assertEqual(info["version"], "10.0")
        self.assertEqual(info["metadata_size"], 65536)
        self.assertEqual(info["slots"], 2)
        self.assertEqual(info["device_size"], 33554432)
        self.assertEqual(info["partitions"]["system"]["size"], 2055 * 512)
        self.assertEqual(info["partitions"]["vendor"]["size"], 4096 * 512)
        self.assertEqual(info["partitions"]["system"]["group"], "dyn")
        self.assertEqual(info["groups"]["dyn"], 23068672)

    def test_reads_virtual_ab_version(self):
        self.assertEqual(lp.parse_dump(V10_2_DUMP)["version"], "10.2")

    def test_missing_sections_are_none_not_errors(self):
        info = lp.parse_dump("")
        self.assertIsNone(info["version"])
        self.assertEqual(info["partitions"], {})
        self.assertEqual(info["groups"], {})

    def test_empty_partition_table_yields_no_partitions(self):
        info = lp.parse_dump(empty_dump())
        self.assertEqual(info["partitions"], {})
        self.assertEqual(info["device_size"], 33554432)

    def test_ignores_free_text_and_banner(self):
        info = lp.parse_dump("Welcome to lpdump\n\n" + V10_DUMP + "\nTrailing junk\n")
        self.assertEqual(lp.parse_dump(V10_DUMP), info)


class CaseAndReportTests(unittest.TestCase):
    def make_case(self, case_id="w/raw-v10.0", tier="required", ok=True, status_skip=None):
        case = lp.Case(case_id, "w", tier, "raw")
        if status_skip:
            case.skip(status_skip)
        else:
            case.check("image: metadata version", ok, "10.0", "10.0" if ok else "9.0")
        return case.finish()

    def test_pass_fail_and_skip_status(self):
        self.assertEqual(self.make_case().status, "pass")
        failed = self.make_case(ok=False)
        self.assertEqual(failed.status, "fail")
        self.assertIn("metadata version", failed.failure)
        skipped = self.make_case(status_skip="unsupported: no --virtual-ab")
        self.assertEqual((skipped.status, skipped.skip_kind), ("skip", "unsupported"))
        self.assertEqual(lp.Case("x/y", "w", "required", "raw").finish().status, "skip")

    def test_to_json_truncates_verbose_output_but_keeps_counts(self):
        case = lp.Case("w/raw", "w", "required", "raw")
        case.commands.append({"argv": ["lpmake"], "rc": 0, "stdout": "x" * 5000,
                              "stderr": "", "ms": 3, "label": "lpmake", "log": "logs/a.log"})
        payload = case.finish().to_json()
        command = payload["commands"][0]
        self.assertEqual(command["stdout_bytes"], 5000)
        self.assertLess(len(command["stdout"]), 2100)
        self.assertEqual(command["argv"], ["lpmake"])

    def test_exit_code_only_counts_required_failures(self):
        passing = self.make_case("w/a")
        optional_failure = self.make_case("w/b", tier="optional", ok=False)
        unsupported = self.make_case("w/c", tier="optional",
                                     status_skip="unsupported: writer lacks --virtual-ab")
        report = lp.build_report({"bin_dir": "/bin"}, [], [passing, optional_failure, unsupported])
        self.assertEqual(report["verdict"]["exit_code"], 0)
        self.assertTrue(report["verdict"]["compatible"])
        self.assertEqual(report["summary"]["failed_optional"], 1)
        self.assertEqual(report["summary"]["skipped_unsupported"], 1)
        self.assertIn("PASS", report["verdict"]["headline"])

        required_failure = self.make_case("w/d", ok=False)
        report = lp.build_report({"bin_dir": "/bin"}, [], [passing, required_failure])
        self.assertEqual(report["verdict"]["exit_code"], 1)
        self.assertFalse(report["verdict"]["compatible"])
        self.assertIn("FAIL", report["verdict"]["headline"])

    def test_summarize_groups_per_writer(self):
        cases = [self.make_case("w/a"), self.make_case("w/b", ok=False),
                 self.make_case("v/a")]
        cases[2].writer = "v"
        summary = lp.summarize(cases)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["writers"]["w"], {"pass": 1, "fail": 1, "skip": 0})
        self.assertEqual(summary["writers"]["v"], {"pass": 1, "fail": 0, "skip": 0})

    def test_markdown_lists_cases_provenance_and_separates_unsupported(self):
        cases = [self.make_case("w/raw-v10.0"), self.make_case("w/vab", tier="optional",
                                                               status_skip="unsupported: no flag")]
        writers = [{"tag": "aosp-10", "path": "/opt/aosp-10/lpmake", "sha256": "a" * 64,
                    "flags": ["--device-size", "--sparse"], "help_first_line": "usage: lpmake"}]
        markdown = lp.render_markdown(lp.build_report({"bin_dir": "/latest", "tools": {}},
                                                      writers, cases))
        self.assertIn("w/raw-v10.0", markdown)
        self.assertIn("aosp-10", markdown)
        self.assertIn("/opt/aosp-10/lpmake", markdown)
        self.assertIn("## Unsupported capabilities", markdown)
        self.assertIn("unsupported: no flag", markdown)
        self.assertIn("## Failures", markdown)
        self.assertIn("Fixture", markdown.replace("Fixtures", "Fixture"))
        self.assertNotIn("## Commands of failed cases\n- ", markdown)


def dump_for(parts, minor=0):
    """lpdump-shaped text for exactly these parts (sizes in sectors from bytes)."""
    table = ["Metadata version: 10.%d" % minor, "Metadata size: 512", "Metadata max size: %d" % lp.META_SIZE,
             "Metadata slot count: %d" % lp.META_SLOTS, "Partition table:",
             "------------------------"]
    layout = ["Super partition layout:", "------------------------"]
    cursor = 2048
    for name, size in parts:
        sectors = size // lp.SECTOR
        table += ["  Name: " + name, "  Group: " + lp.GROUP_NAME, "  Attributes: readonly",
                  "  Extents:", "    0 .. %d linear super %d" % (sectors - 1, cursor), ""]
        layout.append("super: %d .. %d: %s (%d sectors)" % (cursor, cursor + sectors - 1, name,
                                                            sectors))
        cursor += sectors
    return "\n".join(table + layout + [
        "Block device table:", "------------------------", "  Partition name: super",
        "  Size: %d bytes" % lp.DEVICE_SIZE, "", "Group table:", "------------------------",
        "  Name: " + lp.GROUP_NAME, "  Maximum size: %d" % lp.GROUP_SIZE, ""]) + "\n"


class ScriptedCtx:
    """A Ctx stand-in that replays lpdump text and emulates lpunpack's file writes.

    It exists purely to exercise the harness's own check composition; it is not
    evidence that any real tool reads a real super image.
    """

    def __init__(self, tmp, images, dump, tamper=None, slots_ok=(0, 1), write_suffix=""):
        self.tmp, self.images, self.dump, self.tamper = tmp, images, dump, tamper or {}
        self.slots_ok, self.calls = tuple(slots_ok), []
        self.write_suffix = write_suffix
        self.latest = {"lpdump": {"path": "/fake/lpdump"}, "lpunpack": {"path": "/fake/lpunpack"},
                       "simg2img": {"path": "/fake/simg2img"}}
        self.latest_flags = set()

    def run(self, case, label, cmd):
        argv = [str(part) for part in cmd]
        self.calls.append((label, argv))
        res = {"argv": argv, "rc": 0, "stdout": "", "stderr": "", "ms": 0, "label": label}
        if argv[0].endswith("lpdump"):
            res["stdout"] = self.dump
        elif argv[0].endswith("lpunpack"):
            slot = int(argv[1].split("=")[1])
            outdir = Path(argv[3])
            if slot not in self.slots_ok:
                res["rc"], res["stderr"] = 1, "slot %d is not populated" % slot
            else:
                outdir.mkdir(parents=True, exist_ok=True)
                for name in self.images:
                    target = outdir / (name + self.write_suffix + ".img")
                    if self.images[name]["size"] == 0:
                        target.write_bytes(b"")
                        continue
                    payload = Path(self.images[name]["path"]).read_bytes()
                    if name in self.tamper:
                        payload = bytes([payload[0] ^ 0xFF]) + payload[1:]
                    target.write_bytes(payload)
        case.commands.append(res)
        return res

    def ok(self, case, label, cmd):
        res = self.run(case, label, cmd)
        return case.check(label + " exit 0", res["rc"] == 0, 0, res["rc"], res["stderr"])

    def raw_view(self, case, img, storage):
        return None


class VerifyPathTests(TmpDirTest):
    def setUp(self):
        super().setUp()
        self.images = {}
        for name, size in lp.MAIN_PARTS:
            path = self.tmp / ("input " + name + ".img")
            path.write_bytes(lp.make_bytes(size, "verify:" + name))
            self.images[name] = {"path": str(path), "size": size,
                                 "sha256": lp.sha256_file(path)}

    def check_image(self, images=None, parts=None, minor=0, dump_minor=0, require_slot1=True,
                    **kwargs):
        images = images or self.images
        parts = parts or lp.MAIN_PARTS
        ctx = ScriptedCtx(self.tmp, images, dump_for(parts, dump_minor), **kwargs)
        case = lp.Case("w/raw-v10.0", "w", "required", "raw")
        info = lp.verify_super(ctx, case, self.tmp / "super raw.img", parts, images, "image",
                               minor, self.tmp / "verify raw", require_slot1=require_slot1)
        return case.finish(), info, ctx

    def test_matching_partitions_pass_and_both_slots_are_extracted(self):
        case, info, ctx = self.check_image()
        self.assertEqual(case.status, "pass", case.failure)
        self.assertEqual(info["version"], "10.0")
        self.assertEqual(info["device_size"], lp.DEVICE_SIZE)
        slots = [argv[1] for label, argv in ctx.calls if argv[0].endswith("lpunpack")]
        self.assertEqual(slots, ["--slot=0", "--slot=1"])
        tools = {argv[0] for _label, argv in ctx.calls}
        self.assertEqual(tools, {"/fake/lpdump", "/fake/lpunpack"})

    def test_virtual_ab_slot_suffixed_partitions_resolve(self):
        parts = [(name + "_a", size) for name, size in lp.MAIN_PARTS]
        dump = dump_for(parts, minor=2).replace("Group: dyn", "Group: dyn_a").replace(
            "Name: dyn", "Name: dyn_a")
        ctx = ScriptedCtx(self.tmp, self.images, dump, write_suffix="_a")
        case = lp.Case("w/virtual-ab-v10.2", "w", "required", "raw")
        info = lp.verify_super(ctx, case, self.tmp / "super ab.img", lp.MAIN_PARTS, self.images,
                               "image", 2, self.tmp / "verify ab", require_slot1=False)
        self.assertEqual(case.finish().status, "pass", case.failure)
        self.assertEqual(info["version"], "10.2")

    def test_unexpected_extra_partition_fails_the_set_check(self):
        parts = lp.MAIN_PARTS + (("rogue", 4096),)
        dump = dump_for(parts, minor=0)
        images = dict(self.images, rogue={"path": str(self.tmp / "rogue.img"), "size": 4096,
                                          "sha256": "0" * 64})
        (self.tmp / "rogue.img").write_bytes(b"\0" * 4096)
        ctx = ScriptedCtx(self.tmp, images, dump)
        case = lp.Case("w/raw-v10.0", "w", "required", "raw")
        lp.verify_super(ctx, case, self.tmp / "super extra.img", lp.MAIN_PARTS, images,
                        "image", 0, self.tmp / "verify extra")
        self.assertEqual(case.finish().status, "fail")
        failed = [c["name"] for c in case.checks if not c["ok"]]
        self.assertTrue(any("partition set" in name for name in failed), failed)

    def test_wrong_version_fails_the_case(self):
        case, _info, _ctx = self.check_image(dump_minor=2)
        self.assertEqual(case.status, "fail")
        self.assertIn("metadata version", case.failure)

    def test_tampered_partition_bytes_fail_with_hash_mismatch(self):
        case, _info, _ctx = self.check_image(tamper={"vendor": True})
        self.assertEqual(case.status, "fail")
        self.assertIn("vendor.img sha256", case.failure)

    def test_missing_partition_file_fails(self):
        case, _info, _ctx = self.check_image(images={"system": self.images["system"]})
        self.assertEqual(case.status, "fail")
        self.assertIn("vendor.img missing", case.failure)

    def test_unpopulated_slot1_is_noted_when_not_required(self):
        case, _info, _ctx = self.check_image(dump_minor=2, minor=2, require_slot1=False,
                                             slots_ok=(0,))
        self.assertEqual(case.status, "pass", case.failure)
        self.assertTrue(any("slot1" in note and "not required for this image" in note
                            for note in case.notes))

    def test_unpopulated_slot1_fails_a_normal_image(self):
        case, _info, _ctx = self.check_image(slots_ok=(0,))
        self.assertEqual(case.status, "fail")
        self.assertIn("slot1", case.failure)

    def test_zero_length_partition_needs_no_bytes(self):
        parts = lp.MAIN_PARTS + (lp.ZERO_PART,)
        images = dict(self.images, empty={"path": str(self.tmp / "absent.img"), "size": 0,
                                          "sha256": ""})
        case, _info, _ctx = self.check_image(images=images, parts=parts)
        self.assertEqual(case.status, "pass", case.failure)


@unittest.skipUnless(os.name == "posix", "needs an executable script")
class ExecutableProbeTests(TmpDirTest):
    def test_probe_flags_reads_help_output(self):
        exe = write_script(self.tmp / "lpmake", 'echo "usage: lpmake --sparse --virtual-ab --output"\n'
                                                'echo "  --metadata-size N" >&2\nexit 0\n')
        flags, first = lp.probe_flags(exe, 30)
        self.assertEqual(first, "usage: lpmake --sparse --virtual-ab --output")
        self.assertTrue({"--sparse", "--virtual-ab", "--output", "--metadata-size"} <= flags)

    def test_probe_flags_reports_missing_binary_without_inventing_flags(self):
        flags, first = lp.probe_flags(str(self.tmp / "absent"), 30)
        self.assertEqual(flags, set())
        self.assertEqual(first, "")

    def test_run_records_nonzero_exit_and_output(self):
        exe = write_script(self.tmp / "lpunpack", 'echo "unknown option --slot" >&2\nexit 64\n')
        res = lp.run([exe, "--slot=0", "in.img", "outdir"], 30)
        self.assertEqual(res["rc"], 64)
        self.assertIn("unknown option", res["stderr"])
        self.assertEqual(res["argv"], [exe, "--slot=0", "in.img", "outdir"])

    @unittest.skipUnless(shutil.which("python3"), "needs python3 on PATH")
    def test_writer_help_flag_detection_drives_virtual_ab_case(self):
        old = write_script(self.tmp / "lpmake-old",
                           'case "$1" in --help|-h) echo "usage: lpmake --sparse --output"; exit 64;;\n'
                           'esac\nexit 1\n')
        modern = write_script(self.tmp / "lpmake-new",
                              'case "$1" in --help|-h) echo "usage: lpmake --sparse --virtual-ab";;'
                              ' esac\nexit 1\n')
        self.assertNotIn("--virtual-ab", lp.probe_flags(old, 30)[0])
        self.assertIn("--virtual-ab", lp.probe_flags(modern, 30)[0])
        case = lp.Case("w/vab", "w", "required", "raw")
        case.tier = "optional" if "--virtual-ab" not in lp.probe_flags(old, 30)[0] else "required"
        self.assertEqual(case.tier, "optional")


if __name__ == "__main__":
    unittest.main()
