#!/usr/bin/env python3
"""Exercise smoke-test CLI contracts with isolated executable fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

TOOL = r'''#!/usr/bin/env bash
set -euo pipefail
tool=§{0##*/}
printf '%s' "$tool" >> "$MOCK_SMOKE_TRACE"
printf ' <%s>' "$@" >> "$MOCK_SMOKE_TRACE"
printf '\n' >> "$MOCK_SMOKE_TRACE"
if [[ "§{1:-}" = --help || "§{1:-}" = -h ]]; then
  if [[ "§{MOCK_LOADER_FAILURE:-}" = "$tool" ]]; then
    echo 'error while loading shared libraries: fixture.so' >&2
    exit 127
  fi
  if [[ "$tool" = lpunpack && "$1" = --help ]]; then
    echo 'Unrecognized argument.' >&2
    exit 64
  fi
  echo "Usage: $tool [options]" >&2
  [[ "$tool" != lpmake ]] || exit 64
  exit 0
fi
case "$tool" in
  lpmake)
    system= vendor= output=
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --image)
          case "$2" in system=*) system=§{2#system=} ;; vendor=*) vendor=§{2#vendor=} ;; esac
          shift 2 ;;
        --output) output=$2; shift 2 ;;
        *) shift ;;
      esac
    done
    cp "$system" "$output.system"
    cp "$vendor" "$output.vendor"
    truncate -s 67108864 "$output"
    ;;
  lpdump) printf 'Partition name: system\nPartition name: vendor\n' ;;
  lpunpack)
    [[ "§{1:-}" = --slot=0 && $# = 3 ]] || exit 91
    if [[ "§{MOCK_UNPACK_FAILURE:-0}" = 1 ]]; then
      echo 'fixture unpack failure' >&2
      exit 23
    fi
    cp "$2.system" "$3/system.img"
    cp "$2.vendor" "$3/vendor.img"
    if [[ "§{MOCK_CORRUPT:-0}" = 1 ]]; then printf 'bad' > "$3/vendor.img"; fi
    ;;
esac
'''


@unittest.skipUnless(BASH, "Bash is required")
class SmokeTest(unittest.TestCase):
    def run_smoke(self, **settings):
        with tempfile.TemporaryDirectory(prefix="lptools smoke ") as tmp:
            base = Path(tmp)
            bin_dir = base / "bundle with spaces" / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("lpmake", "lpdump", "lpunpack"):
                target = bin_dir / name
                target.write_bytes(TOOL.replace("§", "$" ).encode())
                target.chmod(0o755)
            trace = base / "trace.log"
            env = os.environ.copy()
            for key in ("BASH_ENV", "ENV", "MOCK_LOADER_FAILURE", "MOCK_UNPACK_FAILURE", "MOCK_CORRUPT"):
                env.pop(key, None)
            env.update(settings)
            env["MOCK_SMOKE_TRACE"] = trace.as_posix()
            result = subprocess.run(
                [BASH, "--noprofile", "--norc", str(ROOT / "scripts/smoke-test.sh"), bin_dir.as_posix()],
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
            )
            return result, trace.read_text() if trace.exists() else ""

    def test_nonzero_help_and_short_help_fallback_allow_real_round_trip(self):
        result, trace = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("smoke test: OK", result.stdout)
        self.assertIn("lpmake <--help>", trace)
        self.assertIn("lpunpack <-h>", trace)
        self.assertEqual(trace.count("lpunpack <--slot=0>"), 1)
        self.assertIn("extracted both partitions byte-exactly", result.stdout)

    def test_loader_error_is_not_accepted_as_help(self):
        result, trace = self.run_smoke(MOCK_LOADER_FAILURE="lpdump")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture.so", result.stdout)
        self.assertIn("lpdump cannot print help", result.stdout)
        self.assertNotIn("lpmake <--device-size>", trace)

    def test_unpack_failure_is_reported_without_retry(self):
        result, trace = self.run_smoke(MOCK_UNPACK_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture unpack failure", result.stdout)
        self.assertIn("lpunpack failed", result.stdout)
        self.assertEqual(sum(line.startswith("lpunpack ") and "help>" not in line and "<-h>" not in line
                             for line in trace.splitlines()), 1)

    def test_corrupt_extraction_is_rejected(self):
        result, _ = self.run_smoke(MOCK_CORRUPT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vendor.img is not byte-identical", result.stdout)


if __name__ == "__main__":
    unittest.main()
