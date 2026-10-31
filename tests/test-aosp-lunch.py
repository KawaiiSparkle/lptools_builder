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

    def test_host_setup_is_exported_before_lunch_and_retains_explicit_product(self):
        result = self.run_shell(r'''
lunch() {
  [ "$TARGET_BUILD_UNBUNDLED" = true ] || return 91
  [ "$ALLOW_MISSING_DEPENDENCIES" = true ] || return 92
  [ "$(printenv TARGET_BUILD_UNBUNDLED)" = true ] || return 93
  [ "$(printenv ALLOW_MISSING_DEPENDENCIES)" = true ] || return 94
  [ "$(printenv WITH_DEXPREOPT)" = false ] || return 95
  echo "host-combo:$1"
}
lptools_host_lunch
[ "$LP_LUNCH_COMBO" = lptools_host-trunk_staging-eng ]
lptools_host_lunch custom-ap3a-user
[ "$LP_LUNCH_COMBO" = custom-ap3a-user ]
''')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("host-combo:lptools_host-trunk_staging-eng", result.stdout)
        self.assertIn("host-combo:custom-ap3a-user", result.stdout)
        self.assertEqual(result.stdout.count("trying lunch"), 2)

    def test_temporary_host_product_keeps_existing_device_files_and_cleans_after_failure(self):
        result = self.run_shell(r'''
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/device/lptools-host.user"
printf 'existing\n' > "$work/device/lptools-host.user/sentinel"
lptools_prepare_host_product "$work" "$LPTOOLS_TEST_ROOT/config/host-product" ''
[ -f "$LP_HOST_PRODUCT_DIR/AndroidProducts.mk" ]
[ -f "$LP_HOST_PRODUCT_DIR/lptools_host.mk" ]
saved=$LP_HOST_PRODUCT_DIR
lunch() { return 37; }
if lptools_host_lunch custom-release-user; then exit 99; else rc=$?; fi
[ "$rc" = 37 ]
lptools_cleanup_host_product
[ ! -e "$saved" ]
[ -z "$LP_HOST_PRODUCT_DIR" ]
[ "$(cat "$work/device/lptools-host.user/sentinel")" = existing ]
lptools_prepare_host_product "$work" missing-templates custom-release-user
[ -z "$LP_HOST_PRODUCT_DIR" ]
''')
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_normal_lunch_does_not_force_host_settings(self):
        result = self.run_shell(r'''
unset TARGET_BUILD_UNBUNDLED ALLOW_MISSING_DEPENDENCIES
export WITH_DEXPREOPT=true
lunch() {
  [ -z "${TARGET_BUILD_UNBUNDLED:-}" ] || return 91
  [ -z "${ALLOW_MISSING_DEPENDENCIES:-}" ] || return 92
  [ "$WITH_DEXPREOPT" = true ] || return 93
}
lptools_lunch aosp_arm64
''')
        self.assertEqual(result.returncode, 0, result.stdout)

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
