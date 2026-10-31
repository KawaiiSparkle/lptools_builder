#!/usr/bin/env python3
"""Regression tests for tools/check_projects.py project resolution.

Background: the AOSP 15 manifest declares

    <project path="build/make" name="platform/build" />

required-projects.txt used to list both "platform/build" and
"platform/build/make". The old checker stripped the leading "platform/" from
every required entry, so "platform/build/make" matched the checkout path
"build/make" and was reported present -- while "repo sync platform/build/make"
fails with "error: project platform/build/make not found". The resolver, and
the --print-projects selector that feeds repo sync, must therefore stay exact:
a real manifest project name first, an exact checkout path second, and no
invented alias.

These tests drive the real CLI through subprocess against temporary manifest
fixtures. No repo, git or download is ever run.

Run: python3 tests/test-check-projects.py
"""

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_projects.py"

AOSP15_BUILD = '<project path="build/make" name="platform/build" />'


def stdout_lines(proc):
    return [line for line in proc.stdout.splitlines() if line.strip()]


def load_checker():
    """Import tools/check_projects.py as a module (hyphen-free path required)."""
    spec = importlib.util.spec_from_file_location("check_projects_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProjectResolutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="check projects ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "aosp"
        self.manifests = self.repo / ".repo" / "manifests"
        self.list_file = self.root / "required-projects.txt"

    # -- fixtures ---------------------------------------------------------
    def write_manifest(self, body, includes=None):
        """Write .repo/manifests/manifest.xml (the real repo layout)."""
        self.manifests.mkdir(parents=True, exist_ok=True)
        (self.manifests / "manifest.xml").write_text(
            textwrap.dedent(body).lstrip(), encoding="utf-8")
        for name, content in (includes or {}).items():
            (self.manifests / name).write_text(
                textwrap.dedent(content).lstrip(), encoding="utf-8")

    def write_required(self, lines):
        self.list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_cli(self, *args):
        cmd = [sys.executable, str(SCRIPT), "--repo-root", str(self.repo)]
        if self.list_file.exists():
            cmd += ["--list", str(self.list_file)]
        cmd += list(args)
        return subprocess.run(
            cmd, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")

    def run_json(self):
        proc = self.run_cli("--json")
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return proc, json.loads(proc.stdout)

    # -- name and path selection -----------------------------------------
    def test_name_and_checkout_path_selection_dedupes_to_one_real_name(self):
        self.write_manifest("""
            <manifest>
              <default revision="refs/tags/android-15.0.0_r1" remote="aosp" />
            """ + AOSP15_BUILD + """
              <project path="frameworks/base" name="platform/frameworks/base" />
            </manifest>
        """)
        self.write_required([
            "# platform/build/make is not a repository name; do not add it.",
            "platform/build",
            "build/make",
            "platform/build",
        ])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stdout_lines(proc), ["platform/build"])

    def test_json_reports_requested_project_real_name_and_path(self):
        self.write_manifest("""
            <manifest>
            """ + AOSP15_BUILD + """
              <project path="frameworks/base" name="platform/frameworks/base" />
            </manifest>
        """)
        self.write_required(["build/make", "platform/frameworks/base"])
        proc, data = self.run_json()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["missing"], [])
        self.assertEqual(data["required"], 2)
        self.assertEqual(data["manifest_projects"], 2)
        self.assertEqual(data["resolved"], [
            {"project": "build/make", "name": "platform/build", "path": "build/make"},
            {"project": "platform/frameworks/base",
             "name": "platform/frameworks/base", "path": "frameworks/base"},
        ])
        self.assertEqual(data["sync_projects"],
                         ["platform/build", "platform/frameworks/base"])

    # -- the regression: no platform/ shortcut ----------------------------
    def test_platform_prefixed_checkout_path_is_not_a_project(self):
        self.write_manifest("<manifest>" + AOSP15_BUILD + "</manifest>")
        self.write_required(["platform/build/make"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("platform/build/make", proc.stderr)

        proc, data = self.run_json()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["missing"], ["platform/build/make"])
        self.assertEqual(data["resolved"], [])
        self.assertEqual(data["sync_projects"], [])

    def test_prefix_stripped_lookalike_path_is_not_accepted(self):
        """A project whose name prefix-strips to a different repo's path."""
        self.write_manifest("""
            <manifest>
              <project path="other/kernel" name="platform/kernel" />
            </manifest>
        """)
        self.write_required(["platform/other/kernel"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("platform/other/kernel", proc.stderr)

    # -- include / remove-project ---------------------------------------
    def test_includes_are_followed_and_removed_projects_are_gone(self):
        self.write_manifest("""
            <manifest>
              <include name="aosp.xml" />
              <remove-project name="platform/transient" />
            </manifest>
        """, includes={"aosp.xml": """
            <manifest>
              """ + AOSP15_BUILD + """
              <project path="system/extras" name="platform/system/extras" />
              <project path="transient/thing" name="platform/transient" />
            </manifest>
        """})
        required = ["platform/build", "platform/system/extras", "platform/transient"]
        self.write_required(required)

        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("platform/transient", proc.stderr)

        proc, data = self.run_json()
        self.assertEqual(data["missing"], ["platform/transient"])
        self.assertEqual(data["sync_projects"],
                         ["platform/build", "platform/system/extras"])

        self.write_required(required[:-1])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stdout_lines(proc),
                         ["platform/build", "platform/system/extras"])

    # -- one name, several checkouts ------------------------------------
    def test_same_name_multiple_checkouts_resolve_to_one_name(self):
        self.write_manifest("""
            <manifest>
              <project path="external/libfoo" name="platform/external/libfoo" />
              <project path="vendor/libfoo" name="platform/external/libfoo" />
            </manifest>
        """)
        self.write_required(["platform/external/libfoo"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stdout_lines(proc), ["platform/external/libfoo"])

        proc, data = self.run_json()
        self.assertEqual([row["path"] for row in data["resolved"]],
                         ["external/libfoo", "vendor/libfoo"])
        self.assertEqual({row["name"] for row in data["resolved"]},
                         {"platform/external/libfoo"})
        self.assertEqual(data["sync_projects"], ["platform/external/libfoo"])

        # Selecting one checkout by path collapses onto the same real name.
        self.write_required(["platform/external/libfoo", "vendor/libfoo"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stdout_lines(proc), ["platform/external/libfoo"])

    # -- repo path defaults ---------------------------------------------
    def test_missing_path_attribute_defaults_to_project_name(self):
        self.write_manifest("""
            <manifest>
              <project name="platform/frameworks/base" />
            </manifest>
        """)
        self.write_required(["platform/frameworks/base"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stdout_lines(proc), ["platform/frameworks/base"])

        proc, data = self.run_json()
        self.assertEqual(data["resolved"],
                         [{"project": "platform/frameworks/base",
                           "name": "platform/frameworks/base",
                           "path": "platform/frameworks/base"}])

    def test_default_path_is_not_the_prefix_stripped_name(self):
        self.write_manifest("""
            <manifest>
              <project name="platform/frameworks/base" />
            </manifest>
        """)
        self.write_required(["frameworks/base"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(proc.stdout, "")

    # -- warnings make the selector fail --------------------------------
    def test_print_projects_emits_nothing_when_manifest_has_warnings(self):
        self.write_manifest("""
            <manifest>
              <include name="missing.xml" />
              """ + AOSP15_BUILD + """
            </manifest>
        """)
        self.write_required(["platform/build"])

        proc = self.run_cli("--print-projects")
        self.assertNotEqual(proc.returncode, 0, "a broken include must not yield names")
        self.assertEqual(proc.stdout, "")
        self.assertIn("missing.xml", proc.stderr)

        proc, data = self.run_json()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["missing"], [])
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("missing.xml", data["warnings"][0])
        self.assertEqual(data["sync_projects"], ["platform/build"])

    def test_missing_manifest_is_exit_code_2(self):
        self.write_required(["platform/build"])
        proc = self.run_cli("--print-projects")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("repo init", proc.stderr)

    # -- helper contract -------------------------------------------------
    def test_resolve_required_returns_resolved_and_missing(self):
        checker = load_checker()
        if not hasattr(checker, "resolve_required"):
            self.fail("check_projects.py must expose "
                      "resolve_required(required, projects) -> (resolved, missing)")
        projects = {"platform/build": {"build/make"}}
        resolved, missing = checker.resolve_required(
            ["platform/build", "build/make", "platform/build/make"], projects)
        names = [row["name"] if isinstance(row, dict) else row[0] for row in resolved]
        self.assertEqual(names, ["platform/build", "platform/build"])
        self.assertEqual(missing, ["platform/build/make"])
        self.assertEqual(projects, {"platform/build": {"build/make"}},
                         "resolve_required must not mutate the manifest")

    # -- the checker itself stays offline --------------------------------
    def test_checker_makes_no_network_or_vcs_calls(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("subprocess", "socket", "urllib", "http", "requests",
                       "ftplib", "smtplib", "telnetlib"):
            self.assertNotIn(banned, imported,
                             "check_projects.py must not reach the network or run commands")


if __name__ == "__main__":
    unittest.main(verbosity=2)
