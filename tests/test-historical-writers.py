#!/usr/bin/env python3
"""Offline tests for scripts/build-historical-writers.py.

Network and compiler invocations are mocked or bypassed: these tests cover the
parts that decide *what* gets built and what gets recorded (Soong Android.bp
selection, Gitiles pinning/decoding, toolchain resolution, flag ordering,
writer-list/manifest bookkeeping) without claiming that any historical ref was
actually compiled here.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-historical-writers.py"
SPEC = importlib.util.spec_from_file_location("historical_writers", SCRIPT)
hw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hw)

BP = """
cc_defaults {
    name: "liblp_defaults",
    srcs: [
        "builder.cpp",
        "property_fetcher.cpp",
        "tests/liblp_test.cpp",
    ] + ["writer.cpp"],
    target: {
        host: {
            srcs: ["utility.cpp"],
            exclude_srcs: ["legacy.cpp"],
        },
    },
    // a comment with "quotes" and [brackets]
    export_include_dirs: ["include"],
}
cc_library_static { name: "liblp", defaults: ["liblp_defaults"], }
"""


class RefTagTest(unittest.TestCase):
    def test_release_tags(self):
        self.assertEqual([hw.tag_for("android-%d.0.0_r1" % v) for v in range(10, 17)],
                         ["aosp%d" % v for v in range(10, 17)])

    def test_other_releases_and_refs(self):
        self.assertEqual(hw.tag_for("android-14.0.0_r7"), "aosp14_r7")
        self.assertEqual(hw.tag_for("main"), "main")
        self.assertEqual(hw.tag_for("refs/heads/x y"), "refs-heads-x-y")


class GitilesTest(unittest.TestCase):
    def test_json_guard_is_stripped(self):
        payload = b")]}'\n{\"commit\": \"" + b"a" * 40 + b"\"}"
        self.assertEqual(hw.decode_gitiles_json(payload)["commit"], "a" * 40)
        self.assertEqual(hw.decode_gitiles_json(b'{"commit": "x"}'), {"commit": "x"})

    def test_invalid_json_is_value_error(self):
        with self.assertRaises(ValueError):
            hw.decode_gitiles_json(b")]}'\n{not json")

    def test_base64_blob_with_newlines(self):
        self.assertEqual(hw.decode_gitiles_blob(b"aGVsbG8K\nd29ybGQ="), b"hello\nworld")

    def test_commit_resolution_reads_gitiles_metadata(self):
        with mock.patch.object(hw, "http_get",
                               return_value=b")]}'\n{\"commit\": \"" + b"b" * 40 + b"\"}"):
            resolved = hw.resolve_commit(hw.CORE_REPO, "android-10.0.0_r1")
        self.assertEqual(resolved["commit"], "b" * 40)
        self.assertTrue(resolved["url"].endswith(
            "/platform/system/core/+/android-10.0.0_r1?format=JSON"), resolved["url"])

    def test_short_or_missing_commit_is_rejected(self):
        for payload in (b")]}'\n{}", b")]}'\n{\"commit\": \"abc\"}"):
            with self.subTest(payload=payload):
                with mock.patch.object(hw, "http_get", return_value=payload):
                    with self.assertRaises(hw.BuildError):
                        hw.resolve_commit(hw.CORE_REPO, "android-10.0.0_r1")

    def test_tree_walk_skips_tests_and_hashes_blobs(self):
        def fake_get(url, timeout=60, attempts=3):
            self.assertIn("/+/%s/" % ("c" * 40), url)
            if url.endswith("/fs_mgr/liblp?format=JSON"):
                return b")]}'\n" + json.dumps({"entries": [
                    {"type": "tree", "name": "tests", "id": "t" * 40},
                    {"type": "tree", "name": "include", "id": "i" * 40},
                    {"type": "blob", "name": "Android.bp", "id": "b" * 40},
                ]}).encode()
            if url.endswith("/fs_mgr/liblp/include?format=JSON"):
                return b")]}'\n" + json.dumps({"entries": [
                    {"type": "blob", "name": "liblp.h", "id": "h" * 40}]}).encode()
            return b"ZmlsZQ=="  # b"file"
        with mock.patch.object(hw, "http_get", side_effect=fake_get):
            with tempfile.TemporaryDirectory() as tmp:
                files = hw.fetch_tree(Path(tmp), hw.CORE_REPO, "c" * 40, "fs_mgr/liblp")
                paths = sorted(item["path"] for item in files)
                self.assertEqual(paths, ["fs_mgr/liblp/Android.bp",
                                         "fs_mgr/liblp/include/liblp.h"])
                self.assertTrue(all(item["sha256"] == hw.sha256_bytes(b"file")
                                    for item in files))
                self.assertTrue((Path(tmp) / "platform_system_core" / "fs_mgr/liblp"
                                 / "Android.bp").is_file())


class SoongTest(unittest.TestCase):
    def test_production_sources_and_host_rules(self):
        srcs, includes = hw.select_sources(hw.parse_bp(BP))
        self.assertEqual(srcs, ["builder.cpp", "property_fetcher.cpp", "utility.cpp",
                                "writer.cpp"])
        self.assertEqual(includes, ["include"])

    def test_bp_exclude_is_honoured(self):
        text = BP.replace('    name: "liblp_defaults",',
                          '    name: "liblp_defaults",\n    exclude_srcs: ["property_fetcher.cpp"],')
        srcs, _ = hw.select_sources(hw.parse_bp(text))
        self.assertNotIn("property_fetcher.cpp", srcs)
        self.assertIn("builder.cpp", srcs)

    def test_android_only_sources_never_leak_into_host(self):
        text = BP.replace("target: {", 'target: { android: { srcs: ["android_only.cpp"] },')
        srcs, _ = hw.select_sources(hw.parse_bp(text))
        self.assertNotIn("android_only.cpp", srcs)

    def test_missing_or_empty_defaults_is_an_error(self):
        for text in ("", 'cc_library_static { name: "other", srcs: ["a.cpp"], }',
                     'cc_defaults { name: "liblp_defaults", srcs: [], }'):
            with self.subTest(text=text[:40]):
                with self.assertRaises(hw.BuildError):
                    hw.select_sources(hw.parse_bp(text))

    def test_real_library_shape_and_top_level_variables(self):
        text = '''
        liblp_lib_deps = ["libbase"]
        liblp_lib_deps += ["libsparse"]
        production = ["builder.cpp", "writer.cpp"]
        cc_library {
            name: "liblp", host_supported: true, shared_libs: liblp_lib_deps,
            srcs: production + ["reader.cpp"],
            exclude_srcs: ["writer.cpp"],
            target: { android: { srcs: ["device.cpp"] },
                      host: { srcs: ["utility.cpp"] } },
        }
        cc_test { name: "test", min_shipping_api_level: 29, srcs: ["test.cpp"] }
        '''
        blocks = hw.parse_bp(text)
        self.assertEqual(blocks[0]["shared_libs"], ["libbase", "libsparse"])
        sources, includes = hw.select_sources(blocks)
        self.assertEqual(sources, ["builder.cpp", "reader.cpp", "utility.cpp"])
        self.assertEqual(includes, ["include"])

    def test_test_paths(self):
        for path, expected in (("fs_mgr/liblp/tests/liblp_test.cpp", True),
                               ("fs_mgr/liblp/builder_test.cpp", True),
                               ("fs_mgr/liblp/builder.cpp", False),
                               ("fs_mgr/liblp/testing_helper.cpp", False)):
            self.assertIs(hw.is_test_path(path), expected, path)


class ToolchainTest(unittest.TestCase):
    GLOBAL_GO = """
var (
    ClangDefaultVersion = "clang-r999"
    ClangDefaultShortVersion = "99.0.0"
    DefaultCppStd = "gnu++20"
)
"""

    def test_configured_clang_and_std(self):
        self.assertEqual(hw.parse_global_go(self.GLOBAL_GO),
                         {"clang_version": "clang-r999", "clang_short_version": "99.0.0",
                          "cpp_std": "gnu++20"})

    def test_fallback_when_configured_clang_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            aosp = Path(tmp)
            go = aosp / "build/soong/cc/config/global.go"
            go.parent.mkdir(parents=True)
            go.write_text(self.GLOBAL_GO, encoding="utf-8")
            clang = aosp / "prebuilts/clang/host/linux-x86/clang-r100"
            (clang / "bin").mkdir(parents=True)
            (clang / "bin/clang++").write_text("", encoding="utf-8")
            (clang / "include/c++/v1").mkdir(parents=True)
            resolved = hw.resolve_toolchain(aosp)
            self.assertEqual(Path(resolved["clang++"]), clang / "bin/clang++")
            self.assertTrue(resolved["fallback"])
            self.assertEqual(resolved["cpp_std"], "gnu++20")
            self.assertTrue(Path(resolved["libcxx_include"]).is_dir())

    def test_missing_toolchain_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(hw.BuildError):
                hw.resolve_toolchain(Path(tmp))


class SupportTest(unittest.TestCase):
    def test_moved_libbase_header_dir_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            aosp = Path(tmp)
            (aosp / "system/libbase/include").mkdir(parents=True)
            (aosp / "system/logging/liblog/include").mkdir(parents=True)
            found, missing = hw.support_headers(aosp)
            self.assertIn({"name": "libbase", "path": str(aosp / "system/libbase/include")},
                          found)
            self.assertIn("libsparse", missing)

    def test_missing_libbase_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(hw.BuildError):
                hw.support_headers(Path(tmp))

    def test_required_shared_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            for name in hw.REQUIRED_LIBS:
                (lib / name).write_bytes(b"")
            self.assertEqual({item["name"] for item in hw.support_libs(lib)},
                             set(hw.REQUIRED_LIBS))
            (lib / "libc++.so").unlink()
            with self.assertRaises(hw.BuildError):
                hw.support_libs(lib)


class FlagsTest(unittest.TestCase):
    def test_historical_headers_precede_support_headers(self):
        toolchain = {"cpp_std": "gnu++17", "libcxx_include": "/clang/v1",
                     "clang++": "/clang/bin/clang++"}
        root = Path("/src")
        support = [{"name": "libbase", "path": "/sup/libbase"}]
        flags = hw.compile_flags(toolchain, root, ["include"], support)
        self.assertIn("-D_FILE_OFFSET_BITS=64", flags)
        self.assertIn("-std=gnu++17", flags)
        self.assertLess(flags.index(str(root / "fs_mgr/liblp/include")),
                        flags.index("/sup/libbase"))
        self.assertEqual(flags[-2:], ["-isystem", "/clang/v1"])
        # export_include_dirs repeats the default include dir; keep one -I per dir
        self.assertEqual(flags.count(str(root / "fs_mgr/liblp/include")), 1)


class RecordingTest(unittest.TestCase):
    def test_writer_list_has_one_entry_per_requested_ref(self):
        results = [
            {"tag": "aosp10", "ref": "android-10.0.0_r1", "status": "built",
             "path": "/out/aosp10/bin/lpmake", "sha256": "ab", "writer_arg":
             "aosp10=/out/aosp10/bin/lpmake", "commits": {}, "error": None,
             "log_dir": "/out/logs/aosp10"},
            {"tag": "aosp11", "ref": "android-11.0.0_r1", "status": "failed",
             "path": None, "sha256": None, "writer_arg": None, "commits": {},
             "error": "compile failed", "log_dir": "/out/logs/aosp11"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            payload = hw.write_writer_list(out, ["android-10.0.0_r1", "android-11.0.0_r1"],
                                           results, out, out / "lib64")
            on_disk = json.loads((out / "writer-list.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, payload)
        self.assertEqual(on_disk["summary"], {"built": 1, "failed": 1, "total": 2})
        self.assertEqual([item["tag"] for item in on_disk["writers"]],
                         ["aosp10", "aosp11"])
        self.assertEqual(on_disk["writers"][1]["error"], "compile failed")

    def test_failed_ref_still_writes_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, aosp = Path(tmp) / "out", Path(tmp) / "aosp"
            aosp.mkdir()
            failure = hw.BuildError("synthetic offline failure")
            with mock.patch.object(hw, "resolve_commit", side_effect=failure):
                result = hw.build_ref(aosp, out, "android-10.0.0_r1", "aosp10",
                                      aosp / "bin", aosp / "lib64", 30)
            manifest = json.loads((out / "aosp10/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(manifest["failure"])
        self.assertEqual(manifest["tag"], "aosp10")
        self.assertEqual(manifest["ref"], "android-10.0.0_r1")
        self.assertIn("never", manifest["disclosure"])


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT)] + list(args),
                              capture_output=True, text=True, timeout=30)

    def test_help_lists_the_contract(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--aosp", "--out", "--ref", "--latest-bin"):
            self.assertIn(flag, result.stdout)

    def test_missing_required_arguments_fail(self):
        result = self.run_cli("--ref", "android-10.0.0_r1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--aosp", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)