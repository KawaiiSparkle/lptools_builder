#!/usr/bin/env python3
"""Validate prepare safely: python scripts/test-prepare.py (Bash + PyYAML).

All privileged, allocation, package-manager and network commands are mocks.
Only small test files inside TemporaryDirectory are created or removed. The
legacy free-disk scripts are parsed for syntax only, never executed.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
if BASH is None:
    raise SystemExit("Bash is required for syntax and mock tests")
ACTION = yaml.safe_load((ROOT / ".github/actions/prepare/action.yml").read_text(encoding="utf-8"))
STEPS = {step["name"]: step for step in ACTION["runs"]["steps"]}
SWAP = (ROOT / "scripts/prepare-swap.sh").read_text(encoding="utf-8")

# Functions cannot fall through to host sudo/swapon, even on Linux. File writes
# and removals in the mock dispatcher validate the isolated temporary root.
MOCKS = r'''
set -euo pipefail
record() { printf '%s\t' "$@" >> "$MOCK_ROOT/calls"; printf '\n' >> "$MOCK_ROOT/calls"; }
check_path() { [[ "$1" == "$MOCK_ROOT"/lptools-swap.*/swapfile ]] || { echo "Unsafe mock path: $1" >&2; exit 97; }; }
uname() { echo "${MOCK_OS:-Linux}"; }
getconf() { echo "${MOCK_PAGE_BYTES:-4096}"; }
awk() { command awk "$@"; }
df() {
  record df "$@"
  [[ ${MOCK_DF_FAIL:-0} == 0 ]] || return 28
  if [[ ${MOCK_FALLBACK:-0} == 1 && "${!#}" == /mnt ]]; then
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted\nmock 100 100 0 100%% /mnt\n'
  else
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted\nmock 100000000 0 %s 0%% /\n' "${MOCK_FREE_KIB:-99999999}"
  fi
}
nproc() { record nproc; [[ ${MOCK_OS:-Linux} == Linux ]] || return 127; echo 4; }
free() { record free "$@"; [[ ${MOCK_OS:-Linux} == Linux ]] || return 127; echo 'mock RAM and swap'; }
sysctl() { record sysctl "$@"; [[ ${MOCK_OS:-Linux} == Darwin ]] || return 127; echo 'mock macOS resources'; }
sudo() { record sudo "$@"; dispatch "$@"; }
swapon() { dispatch swapon "$@"; }
dispatch() {
  local tool=$1 path= item= requested= size=
  shift
  case "$tool" in
    mktemp)
      [[ ${MOCK_FAIL:-} != mktemp ]] || return 34
      [[ $1 == -d && $2 == "$MOCK_ROOT/lptools-swap.XXXXXX" ]] || return 97
      path="$MOCK_ROOT/lptools-swap.$RANDOM"
      command mkdir -- "$path" || return
      printf '%s\n' "$path"
      ;;
    install)
      [[ $1 == -m && $2 == 600 && $3 == /dev/null ]] || return 97
      check_path "$4"
      : > "$4"
      [[ ${MOCK_FAIL:-} != install ]] || return 30
      ;;
    fallocate)
      check_path "$3"
      printf 'small mock allocation\n' > "$3"
      printf '%s\n' "${2%M}" > "$MOCK_ROOT/mib"
      [[ ${MOCK_FALLOCATE_FAIL:-0} == 0 ]] || return 29
      ;;
    dd)
      for item in "$@"; do
        case "$item" in of=*) path=${item#of=} ;; count=*) requested=${item#count=} ;; esac
      done
      check_path "$path"
      printf 'small mock dd allocation\n' > "$path"
      printf '%s\n' "$requested" > "$MOCK_ROOT/mib"
      [[ ${MOCK_FAIL:-} != dd ]] || return 31
      ;;
    chmod|mkswap)
      check_path "${!#}"
      [[ ${MOCK_FAIL:-} != "$tool" ]] || return 32
      ;;
    swapon)
      record swapon "$@"
      case "$1" in
        --show=SIZE)
          [[ ${MOCK_FAIL:-} != query ]] || return 36
          printf '%s\n' "${MOCK_SIZES:-}"
          if [[ -f "$MOCK_ROOT/active" ]]; then
            read -r size < "$MOCK_ROOT/mib"
            echo "$((size * 1048576 - ${MOCK_PAGE_BYTES:-4096}))"
          fi
          ;;
        --show=NAME)
          [[ ${MOCK_FAIL:-} != state ]] || return 37
          printf '/mnt/swapfile\n'
          if [[ -f "$MOCK_ROOT/active" ]]; then
            read -r path < "$MOCK_ROOT/active"
            # Match util-linux's escaping of whitespace in raw output.
            printf '%s\n' "${path// /\\x20}"
          fi
          ;;
        --show) echo 'mock active swap table' ;;
        *)
          check_path "$1"
          [[ ${MOCK_FAIL:-} != swapon && ${MOCK_FAIL:-} != state ]] || return 35
          printf '%s\n' "$1" > "$MOCK_ROOT/active"
          if [[ ${MOCK_FAIL:-} == active_error ]]; then return 35; fi
          ;;
      esac
      ;;
    rm)
      [[ $1 == -f && $2 == -- ]] || return 97
      check_path "$3"
      command rm -f -- "$3"
      ;;
    rmdir)
      [[ $1 == -- && $2 == "$MOCK_ROOT"/lptools-swap.* ]] || return 97
      command rmdir -- "$2"
      ;;
    apt-get)
      [[ ${MOCK_FAIL:-} != "apt-$1" ]] || return 41
      ;;
    *) echo "Unexpected privileged mock command: $tool" >&2; return 98 ;;
  esac
}
brew() { record brew "$@"; [[ ${MOCK_FAIL:-} != brew ]] || return 42; }
curl() { record curl "$@"; [[ ${MOCK_FAIL:-} != curl ]] || return 43; }
mkdir() { record mkdir "$@"; }
chmod() { record chmod "$@"; }
git() { record git "$@"; }
'''


def shell_path(path):
    if os.name != "nt":
        return str(path)
    return subprocess.check_output(
        [BASH, "--noprofile", "--norc", "-c", 'cygpath -u -- "$1"', "--", str(path)],
        text=True,
    ).strip()


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test prepare ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.shell_root = shell_path(self.root)
        self.legacy = self.root / "swapfile"
        self.legacy.write_text("original user swap", encoding="utf-8")

    def run_script(self, script=SWAP, **settings):
        env = os.environ.copy()
        for key in ("BASH_ENV", "ENV", "SWAP_DIRECTORY", "SWAP_GB", "EXTRA_PACKAGES"):
            env.pop(key, None)
        env.update(MOCK_ROOT=self.shell_root, SWAP_DIRECTORY=self.shell_root,
                   SWAP_GB="12", EXTRA_PACKAGES="", GITHUB_PATH=self.shell_root + "/github-path")
        env.update({key: str(value) for key, value in settings.items()})
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-s"], input=MOCKS + "\n" + script,
            env=env, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "original user swap")
        return result

    def calls(self):
        file = self.root / "calls"
        return [line.rstrip("\t").split("\t") for line in file.read_text().splitlines()] if file.exists() else []

    def allocations(self):
        return list(self.root.glob("lptools-swap.*"))

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_syntax_and_input_boundaries(self):
        self.assertEqual(ACTION["runs"]["using"], "composite")
        for name, step in STEPS.items():
            with self.subTest(step=name):
                result = subprocess.run([BASH, "-n"], input=step["run"], text=True, capture_output=True)
                self.assert_success(result)
                self.assertNotIn("${{", step["run"], "Pass action inputs through env")
        for path in ("scripts/prepare-swap.sh", "scripts/free-disk.sh", "scripts/free-disk-darwin.sh"):
            with self.subTest(path=path):
                self.assert_success(subprocess.run([BASH, "-n"], input=(ROOT / path).read_text(),
                                                   text=True, capture_output=True))
        self.assertIn('bash "$GITHUB_WORKSPACE/scripts/free-disk.sh"',
                      STEPS["Reclaim disk space (Linux)"]["run"])
        cleanup_source = (ROOT / "scripts/free-disk.sh").read_text()
        self.assertNotRegex(cleanup_source, r"(?m)^\s*(?:sudo\s+)?(?:swapon|mkswap|fallocate)\b")
        self.assertNotIn("/mnt/swapfile", cleanup_source)
        self.assertEqual(STEPS["Ensure minimum swap (Linux)"]["env"]["SWAP_GB"], "${{ inputs.swap-size-gb }}")
        self.assertEqual(STEPS["Install build dependencies (Linux)"]["env"]["EXTRA_PACKAGES"],
                         "${{ inputs.extra-packages }}")

    def test_disabled_swap_does_not_inspect_or_allocate(self):
        self.assert_success(self.run_script(SWAP_GB="0", MOCK_FAIL="query"))
        self.assertEqual(self.calls(), [])

    def test_invalid_size_fails_before_commands(self):
        for value in ("-1", "1.5", "01", "99999", "12; echo injected", "$(echo injected)"):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_script(SWAP_GB=value).returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_existing_swap_is_summed(self):
        for sizes in (str(12 * 2**30), f"{8 * 2**30}\n{4 * 2**30}", str(16 * 2**30)):
            with self.subTest(sizes=sizes):
                self.assert_success(self.run_script(MOCK_SIZES=sizes))
                self.assertFalse(self.allocations())
                self.assertFalse(any(call[0] == "sudo" for call in self.calls()))

    def test_allocates_only_missing_capacity_and_is_idempotent(self):
        result = self.run_script(MOCK_SIZES=str(8 * 2**30))
        self.assert_success(result)
        self.assertEqual((self.root / "mib").read_text().strip(), "4097")
        self.assertEqual(len(self.allocations()), 1)
        self.assert_success(self.run_script(MOCK_SIZES=str(8 * 2**30)))
        self.assertEqual(len(self.allocations()), 1)
        self.assertEqual(sum(call[:2] == ["sudo", "fallocate"] for call in self.calls()), 1)

    def test_relative_directory_is_normalized(self):
        script = 'cd -- "$MOCK_ROOT"\n' + SWAP
        self.assert_success(self.run_script(script, SWAP_DIRECTORY="."))
        self.assertEqual(len(self.allocations()), 1)

    def test_no_swap_allocates_target_with_header(self):
        self.assert_success(self.run_script())
        self.assertEqual((self.root / "mib").read_text().strip(), "12289")
        self.assertEqual(len(self.allocations()), 1)
        self.assertFalse(any(call[:2] == ["sudo", "rm"] for call in self.calls()))

    def test_fallocate_failure_uses_dd(self):
        self.assert_success(self.run_script(MOCK_FALLOCATE_FAIL=1, MOCK_SIZES=str(8 * 2**30)))
        dd_call = next(call for call in self.calls() if call[:2] == ["sudo", "dd"])
        self.assertIn("count=4097", dd_call)
        self.assertEqual(len(self.allocations()), 1)

    def test_disk_reserve_blocks_allocation(self):
        required = (4097 + 1024) * 1024
        result = self.run_script(MOCK_SIZES=str(8 * 2**30), MOCK_FREE_KIB=required - 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not enough disk space", result.stderr)
        self.assertFalse(any(call[0] == "sudo" for call in self.calls()))
        self.assert_success(self.run_script(MOCK_SIZES=str(8 * 2**30), MOCK_FREE_KIB=required))

    def test_unavailable_mnt_uses_runner_temp(self):
        self.assert_success(self.run_script(SWAP_DIRECTORY="", RUNNER_TEMP=self.shell_root,
                                           MOCK_FALLBACK=1))
        self.assertEqual(len(self.allocations()), 1)

    def test_failures_clean_only_new_allocation(self):
        for failure in ("install", "dd", "chmod", "mkswap", "swapon"):
            with self.subTest(failure=failure):
                result = self.run_script(MOCK_FAIL=failure, MOCK_FALLOCATE_FAIL=int(failure == "dd"))
                expected = {"install": 30, "dd": 31, "chmod": 32, "mkswap": 32, "swapon": 35}[failure]
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertFalse(self.allocations(), result.stdout + result.stderr)
                self.assertIn("Removed the incomplete", result.stdout)

    def test_active_or_unknown_swap_is_never_unlinked(self):
        result = self.run_script(MOCK_FAIL="active_error")
        self.assertEqual(result.returncode, 35)
        self.assertEqual(len(self.allocations()), 1)
        self.assertIn("Retaining possibly active", result.stderr)
        self.assertFalse(any(call[:2] == ["sudo", "rm"] for call in self.calls()))

    def test_failed_state_query_preserves_uncertain_file(self):
        result = self.run_script(MOCK_FAIL="state")
        self.assertEqual(result.returncode, 35)
        self.assertEqual(len(self.allocations()), 1)
        self.assertIn("Retaining possibly active", result.stderr)

    def test_query_failure_and_invalid_data_prevent_allocation(self):
        for setting in ({"MOCK_FAIL": "query"}, {"MOCK_SIZES": "bad"},
                        {"MOCK_FREE_KIB": "bad"}, {"MOCK_DF_FAIL": 1}, {"MOCK_FAIL": "mktemp"}):
            with self.subTest(setting=setting):
                self.assertNotEqual(self.run_script(**setting).returncode, 0)
                self.assertFalse(self.allocations())

    def test_resources_on_linux_and_macos(self):
        for platform in ("Linux", "Darwin"):
            for name in ("Show runner resources", "Show runner resources after preparation"):
                with self.subTest(platform=platform, step=name):
                    self.assert_success(self.run_script(STEPS[name]["run"], MOCK_OS=platform))
        self.assertTrue(any(call[0] == "sysctl" for call in self.calls()))
        self.assertTrue(any(call[0] == "free" for call in self.calls()))

    def test_extra_packages_split_without_shell_evaluation(self):
        self.assert_success(self.run_script(STEPS["Install build dependencies (Linux)"]["run"],
                                           EXTRA_PACKAGES="libfoo-dev  libbar:amd64\nzip"))
        install = next(call for call in self.calls() if call[:3] == ["sudo", "apt-get", "install"])
        self.assertEqual(install[-3:], ["libfoo-dev", "libbar:amd64", "zip"])
        self.assertIn("--", install)

    def test_malicious_and_option_package_inputs_fail_before_apt(self):
        for value in ("$(echo injected)", "zip;echo injected", "--allow-unauthenticated", "*"):
            with self.subTest(value=value):
                result = self.run_script(STEPS["Install build dependencies (Linux)"]["run"], EXTRA_PACKAGES=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_package_install_errors_propagate(self):
        for failure, step in (("apt-update", "Install build dependencies (Linux)"),
                              ("apt-install", "Install build dependencies (Linux)"),
                              ("brew", "Install build dependencies (macOS)")):
            with self.subTest(failure=failure):
                result = self.run_script(STEPS[step]["run"], MOCK_FAIL=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("::endgroup::", result.stdout)
        self.assertEqual(sum(call[0] == "brew" for call in self.calls()), 1)

    def test_repo_download_has_fail_retry_and_propagates_errors(self):
        result = self.run_script(STEPS["Install repo"]["run"], MOCK_FAIL="curl")
        self.assertEqual(result.returncode, 43)
        call = next(call for call in self.calls() if call[0] == "curl")
        self.assertIn("--fail", call)
        self.assertEqual(call[call.index("--retry") + 1], "3")
        self.assertFalse(any(call[0] in ("chmod", "git") for call in self.calls()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
