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
from unittest import mock

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

    def test_complete_required_coverage_keeps_optional_outcomes_separate(self):
        writers = [{"tag": "w", "flags": []}]
        cases = [self.make_case("w/raw-v10.0"), self.make_case("w/sparse-v10.0"),
                 self.make_case("w/b", tier="optional", ok=False),
                 self.make_case("w/c", tier="optional",
                                status_skip="unsupported: writer lacks --virtual-ab")]
        report = lp.build_report({"bin_dir": "/bin"}, writers, cases)
        self.assertEqual(report["verdict"]["exit_code"], 0)
        self.assertTrue(report["verdict"]["compatible"])
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["coverage_errors"], [])
        self.assertEqual(report["summary"]["failed_optional"], 1)
        self.assertEqual(report["summary"]["skipped_unsupported"], 1)

        cases.append(self.make_case("w/d", ok=False))
        report = lp.build_report({"bin_dir": "/bin"}, writers, cases)
        self.assertEqual(report["verdict"]["exit_code"], 1)
        self.assertFalse(report["verdict"]["compatible"])

    def test_required_coverage_rejects_missing_skipped_duplicate_or_optional_case(self):
        writers = [{"tag": "w", "flags": []}]
        raw = self.make_case("w/raw-v10.0")
        sparse = self.make_case("w/sparse-v10.0")
        variants = [[], [raw], [raw, sparse, sparse],
                    [raw, self.make_case("w/sparse-v10.0", status_skip="unsupported")],
                    [raw, self.make_case("w/sparse-v10.0", tier="optional")]]
        for cases in variants:
            with self.subTest(ids=[case.id for case in cases]):
                report = lp.build_report({"bin_dir": "/bin"}, writers, cases)
                self.assertEqual(report["verdict"]["exit_code"], 1)
                self.assertFalse(report["coverage_complete"])
        self.assertFalse(lp.build_report({}, [], [])["verdict"]["compatible"])

    def test_virtual_ab_is_required_when_writer_advertises_it(self):
        writers = [{"tag": "w", "flags": ["--virtual-ab"]}]
        cases = [self.make_case("w/raw-v10.0"), self.make_case("w/sparse-v10.0")]
        self.assertTrue(lp.required_coverage(writers, cases))
        cases.append(self.make_case("w/virtual-ab-v10.2"))
        self.assertEqual(lp.required_coverage(writers, cases), [])

    def test_later_skip_never_hides_a_failed_check(self):
        case = lp.Case("w/sparse-v10.0", "w", "required", "sparse")
        case.check("slot1 bytes", False, "input hash", "wrong hash")
        case.skip("unsupported: sparse repack unavailable")
        self.assertEqual(case.finish().status, "fail")
        self.assertIn("slot1 bytes", case.failure)

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

    def test_backup_suffix_does_not_allow_an_unverified_partition(self):
        for minor, suffix in ((0, ""), (2, "_a")):
            with self.subTest(minor=minor):
                parts = [(name + suffix, size) for name, size in lp.MAIN_PARTS]
                parts.append(("system_b", 4096))
                ctx = ScriptedCtx(self.tmp, self.images, dump_for(parts, minor), write_suffix=suffix)
                case = lp.Case("w/extra-backup", "w", "required", "raw")
                lp.verify_super(ctx, case, self.tmp / "extra-backup.img", lp.MAIN_PARTS,
                                self.images, "image", minor, self.tmp / "extra backup")
                self.assertEqual(case.finish().status, "fail")
                self.assertIn("partition set", case.failure)

    def test_virtual_ab_empty_backup_partition_is_allowed(self):
        parts = [(name + "_a", size) for name, size in lp.MAIN_PARTS]
        dump = dump_for(parts, minor=2).replace(
            "Super partition layout:",
            "  Name: system_b\n  Group: dyn_b\n  Attributes: readonly\n  Extents:\n\n"
            "Super partition layout:")
        ctx = ScriptedCtx(self.tmp, self.images, dump, write_suffix="_a")
        case = lp.Case("w/empty-backup", "w", "required", "raw")
        lp.verify_super(ctx, case, self.tmp / "empty-backup.img", lp.MAIN_PARTS,
                        self.images, "image", 2, self.tmp / "empty backup")
        self.assertEqual(case.finish().status, "pass", case.failure)

    def test_raw_image_cannot_substitute_slot_suffixed_partition_names(self):
        parts = [(name + "_a", size) for name, size in lp.MAIN_PARTS]
        ctx = ScriptedCtx(self.tmp, self.images, dump_for(parts), write_suffix="_a")
        case = lp.Case("w/wrong-names", "w", "required", "raw")
        lp.verify_super(ctx, case, self.tmp / "wrong-names.img", lp.MAIN_PARTS,
                        self.images, "image", 0, self.tmp / "wrong names")
        self.assertEqual(case.finish().status, "fail")
        self.assertIn("partition set", case.failure)

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


class EvidenceIsolationTests(TmpDirTest):
    def test_desparse_never_reuses_an_existing_raw_file(self):
        stale = self.tmp / "raw view.img"
        stale.write_bytes(b"old payload")
        case = lp.Case("w/sparse", "w", "required", "sparse")
        ctx = mock.Mock(latest={"simg2img": {"path": "/fake/simg2img"}})
        ctx.run.return_value = {"rc": 1, "stderr": "current sparse image corrupt"}
        result = lp.ensure_raw(ctx, case, self.tmp / "current sparse.img", "desparse", stale)
        self.assertIsNone(result)
        ctx.run.assert_called_once()
        current = Path(ctx.run.call_args.args[2][-1])
        self.assertNotEqual(current, stale)
        self.assertFalse(current.exists())
        self.assertEqual(stale.read_bytes(), b"old payload")
        self.assertEqual(case.finish().status, "fail")

    def test_successful_desparse_must_write_a_new_nonempty_file(self):
        for payload in (None, b"", b"new raw"):
            with self.subTest(payload=payload):
                case = lp.Case("w/sparse", "w", "required", "sparse")
                ctx = mock.Mock(latest={"simg2img": {"path": "/fake/simg2img"}})
                def convert(_case, _label, argv):
                    if payload is not None:
                        Path(argv[-1]).write_bytes(payload)
                    return {"rc": 0, "stderr": ""}
                ctx.run.side_effect = convert
                result = lp.ensure_raw(ctx, case, self.tmp / "current.img", "desparse",
                                       self.tmp / "view.img")
                self.assertEqual(result is not None, bool(payload))
                self.assertEqual(case.finish().status, "pass" if payload else "fail")

    def test_success_exit_cannot_reuse_old_extracted_partition_files(self):
        stale = self.tmp / "slot0"
        stale.mkdir()
        payload = b"old partition"
        (stale / "system.img").write_bytes(payload)
        images = {"system": {"sha256": lp.sha256_file(stale / "system.img")}}
        case = lp.Case("w/raw", "w", "required", "raw")
        ctx = mock.Mock(latest={"lpunpack": {"path": "/fake/lpunpack"}})
        ctx.run.return_value = {"rc": 0, "stderr": ""}
        result = lp.unpack_slot(ctx, case, self.tmp / "current.img", 0,
                                [("system", len(payload))], images, "extract", stale, False, None)
        self.assertFalse(result["ok"])
        self.assertIn("system.img missing", result["problems"])
        self.assertEqual((stale / "system.img").read_bytes(), payload)
        self.assertNotEqual(Path(ctx.run.call_args.args[2][-1]), stale)

    def test_nonempty_output_is_rejected_before_tool_resolution(self):
        evidence = self.tmp / "old report.json"
        evidence.write_text("previous run", encoding="utf-8")
        with mock.patch.object(lp, "resolve_latest") as resolve:
            with self.assertRaises(SystemExit) as caught:
                lp.main(["--out", str(self.tmp), "--latest-bin", "/missing", "--writer", "w=/missing"])
        self.assertEqual(caught.exception.code, 2)
        resolve.assert_not_called()
        self.assertEqual(evidence.read_text(encoding="utf-8"), "previous run")

    def test_sparse_repack_capability_gap_cannot_hide_prior_failures(self):
        case = lp.Case("w/sparse-v10.0", "w", "required", "sparse")
        case.check("slot1 bytes", False, "input hash", "wrong hash")
        ctx = mock.Mock(latest={}, latest_flags=set())
        lp.verify_repack(ctx, case, self.tmp / "repack.img", [], {}, 0, sparse=True)
        self.assertEqual(case.finish().status, "fail")
        self.assertIn("slot1 bytes", case.failure)

    def test_virtual_ab_repack_capability_is_required(self):
        ctx = mock.Mock(latest={"lpmake": {"path": "/fake/latest-lpmake"}}, latest_flags=set())
        def write_vab(case, _label, argv):
            Path(argv[-1]).write_bytes(b"fixture")
            return {"rc": 0, "stderr": ""}
        ctx.run.side_effect = write_vab
        info = {"extracted_images": {"system": {"path": "extracted"}}}
        writer = {"tag": "w", "path": "/fake/historical-lpmake", "flags": ["--virtual-ab"]}
        with mock.patch.object(lp, "build_super", return_value=False), \
                mock.patch.object(lp, "verify_super", return_value=info), \
                mock.patch.object(lp, "verify_repack") as repack:
            cases = lp.run_writer(ctx, writer, self.tmp / "writer")
        vab = next(case for case in cases if case.id == "w/virtual-ab-v10.2")
        self.assertEqual(vab.status, "fail")
        self.assertIn("virtual A/B repack", vab.failure)
        repack.assert_not_called()


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
