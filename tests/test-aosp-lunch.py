#!/usr/bin/env python3
"""Check lunch selection, diagnostics and exports without an AOSP checkout."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required")
class LunchTest(unittest.TestCase):
    def run_shell(self, script):
        env = os.environ.copy()
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        env["LPTOOLS_TEST_ROOT"] = ROOT.as_posix()
        prelude = r'''
set -euo pipefail
log() { printf 'LOG:%s\n' "$*"; }
warn() { printf 'WARN:%s\n' "$*" >&2; }
source "$LPTOOLS_TEST_ROOT/scripts/aosp-lunch.sh"
'''
        return subprocess.run([BASH, "--noprofile", "--norc", "-c", prelude + script],
                              env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=15)

    def test_modern_first_retains_exports_in_same_shell(self):
        result = self.run_shell(r'''
original_pid=$BASHPID
lunch() {
  [ "$BASHPID" = "$original_pid" ] || return 99
  [ "$1" = aosp_arm64-trunk_staging-eng ] || return 98
  export TARGET_PRODUCT=aosp_arm64
  export ANDROID_HOST_OUT='output with spaces/host'
}
lptools_lunch aosp_arm64
[ "$LP_LUNCH_COMBO" = aosp_arm64-trunk_staging-eng ]
[ "$TARGET_PRODUCT" = aosp_arm64 ]
[ "$ANDROID_HOST_OUT" = 'output with spaces/host' ]
''')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("trying lunch"), 1)

    def test_legacy_input_tries_modern_then_legacy_and_keeps_both_outputs(self):
        result = self.run_shell(r'''
lunch() {
  echo "diagnostic:$1" >&2
  case "$1" in
    aosp_arm64-trunk_staging-userdebug) return 21 ;;
    aosp_arm64-userdebug) export TARGET_RELEASE=legacy ;;
    *) return 99 ;;
  esac
}
lptools_lunch aosp_arm64-userdebug
[ "$LP_LUNCH_COMBO" = aosp_arm64-userdebug ]
[ "$TARGET_RELEASE" = legacy ]
''')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("diagnostic:aosp_arm64-trunk_staging-userdebug", result.stdout)
        self.assertIn("diagnostic:aosp_arm64-userdebug", result.stdout)
        self.assertIn("failed (exit 21)", result.stdout)
        self.assertEqual(result.stdout.count("trying lunch"), 2)

    def test_explicit_release_is_attempted_exactly_once(self):
        result = self.run_shell(r'''
lunch() { echo "original-release:$1" >&2; return 37; }
if lptools_lunch aosp_arm64-ap3a-user; then exit 99; else rc=$?; fi
[ -z "$LP_LUNCH_COMBO" ]
exit "$rc"
''')
        self.assertEqual(result.returncode, 37, result.stdout)
        self.assertEqual(result.stdout.count("trying lunch"), 1)
        self.assertIn("original-release:aosp_arm64-ap3a-user", result.stdout)
        self.assertNotIn("trunk_staging", result.stdout)

    def test_each_failure_is_visible_and_last_status_is_preserved(self):
        result = self.run_shell(r'''
lunch() {
  echo "bootstrap-error:$1" >&2
  case "$1" in *-trunk_staging-eng) return 17 ;; *) return 29 ;; esac
}
if lptools_lunch aosp_arm64; then exit 99; else exit $?; fi
''')
        self.assertEqual(result.returncode, 29, result.stdout)
        self.assertIn("bootstrap-error:aosp_arm64-trunk_staging-eng", result.stdout)
        self.assertIn("bootstrap-error:aosp_arm64-eng", result.stdout)
        self.assertIn("failed (exit 17)", result.stdout)
        self.assertIn("failed (exit 29)", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
