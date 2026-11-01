#!/usr/bin/env python3
"""Offline orchestration regressions; these do not prove real LP compatibility.

Run with: python -B tests/test-compatibility-runner.py
"""
from __future__ import annotations

import copy
import importlib.util
import io
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest import mock


RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts/run-lp-compatibility.py"
SPEC = importlib.util.spec_from_file_location("lp_compatibility_runner_under_test", RUNNER_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

# Keep the required refs independent of the production constant.
EXPECTED_REFS = (
    "android-10.0.0_r1",
    "android-11.0.0_r1",
    "android-12.0.0_r1",
    "android-13.0.0_r1",
    "android-14.0.0_r1",
    "android-15.0.0_r1",
    "android-16.0.0_r1",
)


class CoverageCompleteTests(unittest.TestCase):
    def setUp(self):
        self.writers = [
            {
                "ref": ref,
                "status": "built",
                "tag": "writer-android-%d" % version,
                "sha256": "%064x" % version,
            }
            for version, ref in enumerate(EXPECTED_REFS, start=10)
        ]
        self.result = {
            "writers": [
                {"tag": writer["tag"], "sha256": writer["sha256"]}
                for writer in self.writers
            ],
            "coverage_complete": True,
            "coverage_errors": [],
            "verdict": {"compatible": True},
        }

    def assert_rejected(self, *, writers=None, result=None, build_rc=0, matrix_rc=0):
        self.assertIs(
            RUNNER.coverage_complete(
                self.writers if writers is None else writers,
                build_rc,
                matrix_rc,
                self.result if result is None else result,
            ),
            False,
        )

    def test_exact_android_10_through_16_refs_are_required(self):
        self.assertEqual(RUNNER.EXPECTED_REFS, set(EXPECTED_REFS))

    def test_complete_built_matching_coverage_passes(self):
        self.assertIs(RUNNER.coverage_complete(self.writers, 0, 0, self.result), True)

    def test_writer_and_matrix_order_do_not_matter(self):
        self.result["writers"] = self.result["writers"][2:] + self.result["writers"][:2]
        self.assertIs(
            RUNNER.coverage_complete(list(reversed(self.writers)), 0, 0, self.result), True
        )

    def test_each_missing_historical_ref_fails(self):
        for index, ref in enumerate(EXPECTED_REFS):
            with self.subTest(ref=ref):
                self.assert_rejected(writers=self.writers[:index] + self.writers[index + 1:])

    def test_duplicate_historical_ref_with_seven_entries_fails(self):
        for index, ref in enumerate(EXPECTED_REFS):
            with self.subTest(ref=ref):
                writers = copy.deepcopy(self.writers)
                writers[index]["ref"] = writers[(index + 1) % len(writers)]["ref"]
                self.assert_rejected(writers=writers)

    def test_substituted_historical_ref_with_seven_entries_fails(self):
        for replacement in ("android-9.0.0_r1", "android-17.0.0_r1", "android-16.0.0_r2"):
            with self.subTest(replacement=replacement):
                writers = copy.deepcopy(self.writers)
                writers[-1]["ref"] = replacement
                self.assert_rejected(writers=writers)

    def test_extra_historical_ref_fails(self):
        extra = dict(self.writers[-1], ref="android-17.0.0_r1")
        self.assert_rejected(writers=self.writers + [extra])

    def test_every_writer_must_have_built_status(self):
        for index, ref in enumerate(EXPECTED_REFS):
            for status in ("failed", "skipped", "building", ""):
                with self.subTest(ref=ref, status=status):
                    writers = copy.deepcopy(self.writers)
                    writers[index]["status"] = status
                    self.assert_rejected(writers=writers)

    def test_nonzero_or_missing_build_exit_fails(self):
        for build_rc in (1, 2, -1, None):
            with self.subTest(build_rc=build_rc):
                self.assert_rejected(build_rc=build_rc)

    def test_nonzero_or_missing_matrix_exit_fails(self):
        for matrix_rc in (1, 2, -1, None):
            with self.subTest(matrix_rc=matrix_rc):
                self.assert_rejected(matrix_rc=matrix_rc)

    def test_missing_matrix_result_fails(self):
        for result in (None, {}):
            with self.subTest(result=result):
                self.assertIs(RUNNER.coverage_complete(self.writers, 0, 0, result), False)

    def test_missing_matrix_writer_listing_fails(self):
        del self.result["writers"]
        self.assert_rejected()

    def test_each_missing_matrix_writer_fails(self):
        for index, ref in enumerate(EXPECTED_REFS):
            with self.subTest(ref=ref):
                result = copy.deepcopy(self.result)
                del result["writers"][index]
                self.assert_rejected(result=result)

    def test_duplicate_matrix_writer_with_seven_entries_fails(self):
        for index, ref in enumerate(EXPECTED_REFS):
            with self.subTest(ref=ref):
                result = copy.deepcopy(self.result)
                result["writers"][index] = dict(result["writers"][(index + 1) % 7])
                self.assert_rejected(result=result)

    def test_matrix_coverage_must_be_literal_true(self):
        for coverage in (False, None, 0, 1, "true"):
            with self.subTest(coverage=coverage):
                result = copy.deepcopy(self.result)
                result["coverage_complete"] = coverage
                self.assert_rejected(result=result)
        del self.result["coverage_complete"]
        self.assert_rejected()

    def test_matrix_coverage_errors_must_be_present_and_empty(self):
        for errors in (["missing historical writer"], None, ""):
            with self.subTest(errors=errors):
                result = copy.deepcopy(self.result)
                result["coverage_errors"] = errors
                self.assert_rejected(result=result)
        del self.result["coverage_errors"]
        self.assert_rejected()

    def test_false_compatibility_verdict_fails(self):
        self.result["verdict"]["compatible"] = False
        self.assert_rejected()

    def test_each_mismatched_matrix_tag_fails(self):
        for index, ref in enumerate(EXPECTED_REFS):
            with self.subTest(ref=ref):
                result = copy.deepcopy(self.result)
                result["writers"][index]["tag"] += "-substituted"
                self.assert_rejected(result=result)

    def test_each_mismatched_matrix_sha256_fails(self):
        for index, ref in enumerate(EXPECTED_REFS):
            with self.subTest(ref=ref):
                result = copy.deepcopy(self.result)
                result["writers"][index]["sha256"] = "f" * 64
                self.assert_rejected(result=result)

    def test_matching_tag_and_sha_sets_with_swapped_pairs_fail(self):
        first, second = self.result["writers"][:2]
        first["sha256"], second["sha256"] = second["sha256"], first["sha256"]
        self.assert_rejected()

    def test_changed_historical_identity_invalidates_matrix(self):
        for key, replacement in (("tag", "replacement-tag"), ("sha256", "f" * 64)):
            with self.subTest(key=key):
                writers = copy.deepcopy(self.writers)
                writers[0][key] = replacement
                self.assert_rejected(writers=writers)


class MainOutputGuardTests(unittest.TestCase):
    def assert_output_rejected_before_subprocess(self, root, out):
        stderr = io.StringIO()
        # The absent inputs also prove rejection precedes reading a real manifest.
        argv = [
            "--aosp", str(root / "absent-aosp"),
            "--latest-bin", str(root / "absent-latest" / "bin"),
            "--out", str(out),
        ]
        # Replace the whole subprocess module so no subprocess entry point can run.
        with mock.patch.object(RUNNER, "subprocess") as process, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                RUNNER.main(argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(process.mock_calls, [])
        self.assertIn("--out must be an empty directory", stderr.getvalue())

    def test_nonempty_output_directory_is_rejected_without_subprocess(self):
        for entry_kind in ("file", "directory"):
            with self.subTest(entry_kind=entry_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                out = root / "previous-evidence"
                out.mkdir()
                marker = out / "keep-me"
                if entry_kind == "file":
                    marker.write_bytes(b"previous evidence\n")
                else:
                    marker.mkdir()
                self.assert_output_rejected_before_subprocess(root, out)
                self.assertEqual(list(out.iterdir()), [marker])
                if entry_kind == "file":
                    self.assertEqual(marker.read_bytes(), b"previous evidence\n")
                else:
                    self.assertTrue(marker.is_dir())
                    self.assertEqual(list(marker.iterdir()), [])

    def test_existing_output_file_is_rejected_without_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "previous-evidence.json"
            out.write_bytes(b"previous evidence\n")
            self.assert_output_rejected_before_subprocess(root, out)
            self.assertEqual(out.read_bytes(), b"previous evidence\n")


if __name__ == "__main__":
    unittest.main()
