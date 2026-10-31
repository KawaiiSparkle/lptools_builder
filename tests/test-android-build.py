#!/usr/bin/env python3
"""Exercise Android orchestration with a fake AOSP tree; never fetch or compile.

Run: python3 tests/test-android-build.py
Requires Bash and its usual coreutils (Linux or Git Bash on Windows).
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required")
class AndroidBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="android build mock ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "source tree"
        self.out = self.root / "dist tree"
        (self.root / "tmp").mkdir()
        scripts = self.root / "fixture" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("build-android.sh", "log.sh", "aosp-meta.sh",
                     "make-manifest.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        self.put("source tree/.repo/manifest.xml",
                 '<manifest><default revision="mock-branch"/></manifest>\n')
        self.put("mock-bin/uname", "#!/usr/bin/env bash\nprintf 'Linux\\n'\n")
        self.put("mock-bin/git", "#!/usr/bin/env bash\nprintf 'mock-builder\\n'\n")
        self.put("mock-bin/python3", "#!/usr/bin/env bash\nexec " +
                 shlex.quote(Path(sys.executable).as_posix()) + ' "$@"\n')
        self.put("mock-bin/readelf", r'''
            #!/usr/bin/env bash
            printf ' 0x1 (NEEDED) Shared library: [libfixture.so]\n'
        ''')
        self.put("mock-bin/repo", r'''
            #!/usr/bin/env bash
            set -eu
            if [ "${1:-}" = --version ]; then echo 'repo version mock'; exit 0; fi
            [ "$1" = manifest ] || exit 98
            while [ $# -gt 0 ]; do
              if [ "$1" = -o ]; then output=$2; break; fi
              shift
            done
            case "$output" in /*) ;; *) exit 97 ;; esac
            printf 'manifest:%s:%s\n' "$PWD" "$output" >> "$TRACE"
            printf '<manifest><project name="mock" revision="123"/></manifest>\n' > "$output"
        ''')
        self.put("fixture/scripts/build-linux.sh", r'''
            #!/usr/bin/env bash
            set -euo pipefail
            . "$(dirname "$0")/log.sh"
            trap 'rc=$?; trap - EXIT; lp_finish "$rc" || true; exit "$rc"' EXIT
            [ "${LP_NO_GROUPS:-0}" != 1 ] || exit 96
            while [ $# -gt 0 ]; do
              if [ "$1" = --work ]; then
                case "$2" in /*) ;; *) exit 95 ;; esac
                printf 'sync-work:%s\n' "$2" >> "$TRACE"
              fi
              shift
            done
            group_start 'mock child sync'
            run_beat 'mock sync' bash -c 'exit "${MOCK_SYNC_RC:-0}"'
            group_end
        ''')
        self.put("source tree/build/envsetup.sh", r'''
            printf 'source:%s\n' "$BASHPID" >> "$TRACE"
            [ "${MOCK_SOURCE_RC:-0}" = 0 ] || return "$MOCK_SOURCE_RC"
            sleep "${MOCK_SETUP_DELAY:-0}"
            lunch() {
              printf 'lunch:%s:%s\n' "$1" "$BASHPID" >> "$TRACE"
              sleep "${MOCK_LUNCH_DELAY:-0}"
              [ "${MOCK_LUNCH_RC:-0}" = 0 ] || return "$MOCK_LUNCH_RC"
              if [ "${MOCK_MODERN:-0}" = 1 ]; then
                case "$1" in *-trunk_staging-eng) ;; *) return 21 ;; esac
              else
                case "$1" in *-trunk_staging-eng) return 22 ;; *-eng) ;; *) return 23 ;; esac
              fi
              LUNCH_PID=$BASHPID
              MOCK_SELECTED=$1
              local abi=${1#aosp_}
              abi=${abi%%-*}
              # Deliberately differs from the product name and normal out/ path.
              export ANDROID_PRODUCT_OUT="$PWD/$OUT_DIR/target/product/generic_$abi"
              export OUT=$ANDROID_PRODUCT_OUT
            }
            m() {
              [ "$BASHPID" = "$LUNCH_PID" ] || return 94
              printf 'compile:%s:%s\n' "$MOCK_SELECTED" "$BASHPID" >> "$TRACE"
              [ "${MOCK_BUILD_RC:-0}" = 0 ] || return "$MOCK_BUILD_RC"
              mkdir -p "$ANDROID_PRODUCT_OUT/system/bin" "$ANDROID_PRODUCT_OUT/system/lib64"
              for module in lpmake lpdump lpunpack lpadd simg2img img2simg; do
                [ "$module" != "${MOCK_OMIT_MODULE:-}" ] || continue
                printf '%s\n' "$MOCK_SELECTED" > "$ANDROID_PRODUCT_OUT/system/bin/$module"
              done
              printf '%s\n' "$MOCK_SELECTED" > "$ANDROID_PRODUCT_OUT/system/lib64/libfixture.so"
            }
        ''')

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(textwrap.dedent(content).lstrip("\n"))
        path.chmod(0o755)

    def run_build(self, *extra, sync=False, **settings):
        env = os.environ.copy()
        for key in ("LP_NO_GROUPS", "LPTOOLS_WORK", "LPTOOLS_OUT",
                    "LPTOOLS_ABIS", "BASH_ENV", "ENV"):
            env.pop(key, None)
        env.update(GITHUB_ACTIONS="true", NO_COLOR="1", LP_TS="0",
                   LP_BEAT_SECONDS="1", OUT_DIR="custom artifacts")
        env.update({key: str(value) for key, value in settings.items()})
        # Let Bash form its own absolute paths on both POSIX and Windows/MSYS.
        launcher = ('export PATH="$PWD/mock-bin:$PATH" TRACE="$PWD/trace" '
                    'TMPDIR="$PWD/tmp" GITHUB_STEP_SUMMARY="$PWD/summary.md"; '
                    'exec bash fixture/scripts/build-android.sh "$@"')
        args = [BASH, "-c", launcher, "android-mock",
                "--work", "source tree", "--out", "dist tree",
                "--jobs", "2", "--abi", "arm64"]
        if not sync:
            args.append("--skip-sync")
        result = subprocess.run(args + list(extra), cwd=self.root, env=env,
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
        self.output = result.stdout
        self.trace = (self.root / "trace").read_text() if (self.root / "trace").exists() else ""
        self.summary = ((self.root / "summary.md").read_text()
                        if (self.root / "summary.md").exists() else "")
        return result.returncode

    def assert_clean(self):
        self.assertEqual(list((self.root / "tmp").iterdir()), [], self.output)
        self.assertEqual(self.output.count("::group::"),
                         self.output.count("::endgroup::"), self.output)

    def assert_failure(self, status):
        self.assertIn(f"failed (exit {status})", self.summary, self.output)
        self.assertEqual(self.summary.count("### Build stages"), 1, self.output)
        self.assertFalse(list(self.out.glob("*.tar.gz")))
        self.assert_clean()

    def test_legacy_relative_paths_two_abis_and_product_output(self):
        self.assertEqual(self.run_build("--abi", "arm64,arm"), 0, self.output)
        for abi in ("arm64", "arm"):
            combo = f"aosp_{abi}-eng"
            bundle = self.out / f"lptools-android-{abi}"
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(manifest["build_product"], combo)
            self.assertEqual(manifest["target_abi"], abi)
            self.assertEqual(manifest["aosp_pinned_manifest"]["projects"], 1)
            self.assertEqual((bundle / "bin/lpmake").read_text().strip(), combo)
            self.assertEqual((bundle / "lib/libfixture.so").read_text().strip(), combo)
            with tarfile.open(self.out / f"lptools-android-{abi}.tar.gz") as archive:
                self.assertIn(f"lptools-android-{abi}/manifest.json", archive.getnames())
        self.assertEqual(self.summary.count("### Build stages"), 1, self.output)
        self.assertEqual(self.trace.count("compile:"), 2, self.trace)
        self.assert_clean()

    def test_modern_lunch_fallback_and_setup_heartbeat(self):
        self.assertEqual(self.run_build("--abi", "x86_64", MOCK_MODERN=1,
                                       MOCK_SETUP_DELAY=1.2, MOCK_LUNCH_DELAY=1.2),
                         0, self.output)
        self.assertIn("lunch:aosp_x86_64-eng:", self.trace)
        self.assertIn("lunch:aosp_x86_64-trunk_staging-eng:", self.trace)
        manifest = json.loads((self.out / "lptools-android-x86_64/manifest.json").read_text())
        self.assertEqual(manifest["build_product"], "aosp_x86_64-trunk_staging-eng")
        self.assertIn("still running", self.output)
        source_pid = self.trace.split("source:", 1)[1].splitlines()[0]
        compile_pid = self.trace.split("compile:", 1)[1].splitlines()[0].rsplit(":", 1)[1]
        self.assertEqual(source_pid, compile_pid)
        self.assert_clean()

    def test_sync_child_keeps_groups_and_owns_heartbeat(self):
        self.assertEqual(self.run_build(sync=True), 0, self.output)
        self.assertIn("::group::mock child sync", self.output)
        self.assertEqual(self.output.count("mock sync: started"), 1, self.output)
        self.assertNotIn("repo sync android-", self.output)
        self.assertEqual(self.summary.count("### Build stages"), 2, self.output)
        self.assertIn("sync-work:/", self.trace)
        self.assert_clean()

    def test_sync_failure_preserves_status(self):
        self.assertEqual(self.run_build(sync=True, MOCK_SYNC_RC=43), 43, self.output)
        self.assertNotIn("source:", self.trace)
        self.assert_failure(43)

    def test_envsetup_failure_preserves_status(self):
        self.assertEqual(self.run_build(MOCK_SOURCE_RC=17), 17, self.output)
        self.assertNotIn("lunch:", self.trace)
        self.assert_failure(17)

    def test_lunch_failure_preserves_status(self):
        self.assertEqual(self.run_build(MOCK_LUNCH_RC=29), 29, self.output)
        self.assertNotIn("compile:", self.trace)
        self.assert_failure(29)

    def test_compile_failure_preserves_status(self):
        self.assertEqual(self.run_build(MOCK_BUILD_RC=37), 37, self.output)
        self.assert_failure(37)

    def test_missing_device_binary_does_not_package_host_output(self):
        self.assertEqual(self.run_build(MOCK_OMIT_MODULE="lpmake"), 1, self.output)
        self.assertIn("required Android binaries missing for arm64: lpmake", self.output)
        self.assertIn("host-only Soong modules", self.output)
        self.assert_failure(1)

    def test_envsetup_must_be_file_not_directory(self):
        envsetup = self.work / "build/envsetup.sh"
        envsetup.unlink()
        envsetup.mkdir()
        self.assertEqual(self.run_build(), 1, self.output)
        self.assertIn("no AOSP tree", self.output)
        self.assertNotIn("source:", self.trace)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main(verbosity=2)
