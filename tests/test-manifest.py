#!/usr/bin/env python3
"""Reject empty and unpinned AOSP source records before packaging."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GENERATOR = Path(__file__).resolve().parents[1] / "scripts" / "make-manifest.py"
SHA = "0123456789abcdef0123456789abcdef01234567"
VALID = '<manifest><project name="platform/build" revision="' + SHA + '"/></manifest>\n'


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lptools-manifest-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "lpmake").write_bytes(b"fixture binary")

    def generate(self, xml=VALID, count="1"):
        source = self.root / "aosp-manifest.xml"
        source.unlink(missing_ok=True)
        if xml is not None:
            source.write_text(xml, encoding="utf-8", newline="\n")
        return subprocess.run(
            [sys.executable, str(GENERATOR), "--dir", str(self.root),
             "--aosp-ref", "android-15.0.0_r1", "--aosp-pinned-projects", count],
            capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_valid_source_is_hashed_and_counted(self):
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        source = self.root / "aosp-manifest.xml"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(manifest["aosp_pinned_manifest"],
                         {"file": source.name, "projects": 1, "sha256": digest})
        self.assertIn(digest + "  " + source.name,
                      (self.root / "SHA256SUMS").read_text(encoding="utf-8"))

    def test_missing_empty_and_malformed_source_is_rejected(self):
        for xml in (None, "", "<manifest/>", "<manifest>", "<other/>"):
            with self.subTest(xml=xml):
                result = self.generate(xml)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error:", result.stderr)
                self.assertFalse((self.root / "manifest.json").exists())

    def test_unpinned_revisions_are_rejected(self):
        for revision in ("main", "refs/heads/main", "123", "f" * 39):
            with self.subTest(revision=revision):
                result = self.generate(VALID.replace(SHA, revision))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not pinned", result.stderr)

    def test_invalid_and_mismatched_counts_are_rejected(self):
        for count in ("0", "2", "-1", "bad"):
            with self.subTest(count=count):
                result = self.generate(count=count)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "manifest.json").exists())

    def test_unresolved_include_is_rejected(self):
        result = self.generate(VALID.replace("</manifest>", '<include name="more.xml"/></manifest>'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolved projects", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
