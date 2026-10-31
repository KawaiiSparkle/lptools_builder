#!/usr/bin/env python3
"""Isolated behavioral tests for scripts/log.sh; no builds or network access.

Run: python3 tests/test-logging.py
Requires Bash plus its usual coreutils (Git Bash on Windows). Scripts are sent
through subprocess stdin, so no Windows/POSIX command-line quoting is involved.
Each test copies log.sh into a fresh temporary directory whose path has spaces.
The real one-second ticker is exercised; the 15-second default is checked
without waiting for a tick. POSIX process-group cancellation is explicitly
skipped on Windows, where Python cannot deliver the required Unix signals.
"""

import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
POSIX_SIGNALS = os.name == "posix" and hasattr(os, "killpg")


@unittest.skipUnless(BASH, "Bash with coreutils is required (Git Bash on Windows)")
class LoggingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="logging mock with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "fixture").mkdir()
        shutil.copyfile(ROOT / "scripts" / "log.sh", self.root / "fixture" / "log.sh")
        self.summary = self.root / "summary.md"
        self.assertFalse(self.summary.exists(), "summary must not be pre-created")

    def environment(self, settings):
        env = os.environ.copy()
        for key in list(env):
            if (key.startswith(("LP_", "BASH_FUNC_")) or key in {
                    "__LP_LOG_SH", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY",
                    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH",
                    "GLOBIGNORE", "NO_COLOR"}):
                env.pop(key, None)
        env.update(LP_TS="0", LP_BEAT_SECONDS="1", NO_COLOR="1",
                   GITHUB_ACTIONS="false", TERM="xterm", LC_ALL="C")
        for key, value in settings.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def stop_process(self, process):
        """Bound failed tests too, and clean up only this fixture's subprocesses."""
        if POSIX_SIGNALS:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None and os.name == "nt":
            # terminate() alone leaves Git Bash grandchildren holding the pipes.
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def start_shell(self, body, *, source=True, **settings):
        prefix = "set -euo pipefail\n"
        if source:
            prefix += "source fixture/log.sh\n"
        process = subprocess.Popen(
            [BASH, "--noprofile", "--norc", "-s"], cwd=self.root,
            env=self.environment(settings), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=POSIX_SIGNALS)
        self.addCleanup(self.stop_process, process)
        process.stdin.write(prefix + textwrap.dedent(body).lstrip("\n") + "\n")
        process.stdin.close()
        # communicate() otherwise tries to flush a stream we deliberately closed.
        process.stdin = None
        return process

    def collect(self, process, timeout=5):
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.stop_process(process)
            self.fail(f"mock Bash exceeded {timeout}s (possible leaked child/ticker): "
                      f"{error.output!r} {error.stderr!r}")
        return subprocess.CompletedProcess(process.args, process.returncode,
                                           stdout, stderr)

    def run_shell(self, body, *, timeout=5, source=True, **settings):
        started = time.monotonic()
        result = self.collect(self.start_shell(body, source=source, **settings),
                              timeout=timeout)
        self.elapsed = time.monotonic() - started
        return result

    def diagnostic(self, result):
        return (f"status={result.returncode}\nstdout:\n{result.stdout}"
                f"\nstderr:\n{result.stderr}")

    def assert_groups_balanced(self, output, expected):
        depth = 0
        opens = 0
        for line in output.splitlines():
            if line.startswith("::group::"):
                depth += 1
                opens += 1
            elif line == "::endgroup::":
                depth -= 1
                self.assertGreaterEqual(depth, 0, output)
        self.assertEqual(depth, 0, output)
        self.assertEqual(opens, expected, output)

    def test_run_beat_streams_stdout_and_stderr_before_command_finishes(self):
        process = self.start_shell(r'''
            run_beat stream bash -c '
              printf "OUT:first\n"
              printf "ERR:first\n" >&2
              sleep 1.2
              printf "OUT:last\n"
              printf "ERR:last\n" >&2
              : > command.finished
            '
        ''')
        events = queue.Queue()
        captured = {"stdout": [], "stderr": []}

        def drain(name, stream):
            for line in stream:
                captured[name].append(line)
                events.put((name, line, not (self.root / "command.finished").exists()))

        readers = [threading.Thread(target=drain, args=(name, stream), daemon=True)
                   for name, stream in (("stdout", process.stdout),
                                        ("stderr", process.stderr))]
        for reader in readers:
            reader.start()
        early = {}
        deadline = time.monotonic() + 3
        try:
            while len(early) < 2 and time.monotonic() < deadline:
                try:
                    name, line, before_finish = events.get(
                        timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line.rstrip() in {"OUT:first", "ERR:first"}:
                    early[name] = before_finish
            self.assertEqual(early, {"stdout": True, "stderr": True}, captured)
            process.wait(timeout=3)
            for reader in readers:
                reader.join(timeout=1)
                self.assertFalse(reader.is_alive(), "output pipe did not close")
            self.assertEqual(process.returncode, 0, captured)
            self.assertIn("OUT:last\n", captured["stdout"])
            self.assertIn("ERR:last\n", captured["stderr"])
            self.assertNotIn("ERR:first\n", captured["stdout"])
            self.assertNotIn("OUT:first\n", captured["stderr"])
        finally:
            if process.poll() is None:
                self.stop_process(process)
            for reader in readers:
                reader.join(timeout=1)

    def test_run_beat_preserves_success_and_original_nonzero_status(self):
        for status in (0, 7, 43, 127):
            with self.subTest(status=status):
                result = self.run_shell(r'''
                    run_beat status bash -c '
                      printf "child stdout\n"
                      printf "child stderr\n" >&2
                      exit "$MOCK_STATUS"
                    '
                ''', MOCK_STATUS=status)
                self.assertEqual(result.returncode, status, self.diagnostic(result))
                self.assertEqual(result.stdout.count("status: started"), 1)
                self.assertIn("child stdout\n", result.stdout)
                self.assertIn("child stderr\n", result.stderr)

    def test_start_and_one_second_heartbeat_are_visible_while_running(self):
        result = self.run_shell(r'''
            run_beat pulse bash -c 'sleep 1.35; printf "MOCK_DONE\n"'
        ''')
        self.assertEqual(result.returncode, 0, self.diagnostic(result))
        self.assertEqual(result.stdout.count("pulse: started"), 1)
        self.assertRegex(result.stdout, r"\.\.\. pulse still running \(\d+s\)")
        self.assertLess(result.stdout.index("pulse: started"),
                        result.stdout.index("pulse still running"))
        self.assertLess(result.stdout.index("pulse still running"),
                        result.stdout.index("MOCK_DONE"))
        self.assertLess(self.elapsed, 3.5, self.diagnostic(result))

    def test_default_fifteen_seconds_does_not_add_an_end_wait(self):
        result = self.run_shell(r'''
            printf 'DEFAULT=%s\n' "$LP_BEAT_SECONDS"
            run_beat fast bash -c 'sleep 0.05; printf "FAST_DONE\n"'
        ''', LP_BEAT_SECONDS=None, timeout=4)
        self.assertEqual(result.returncode, 0, self.diagnostic(result))
        self.assertIn("DEFAULT=15\n", result.stdout)
        self.assertIn("fast: started", result.stdout)
        self.assertIn("FAST_DONE\n", result.stdout)
        self.assertNotIn("still running", result.stdout)
        self.assertLess(self.elapsed, 3, "short command waited for the default ticker")

    def test_zero_and_noninteger_intervals_fail_before_launching_command(self):
        for interval in ("0", "00", "-1", "1.5", "noninteger"):
            with self.subTest(interval=interval):
                result = self.run_shell(r'''
                    run_beat invalid bash -c 'printf "CHILD_MUST_NOT_RUN\n"'
                ''', LP_BEAT_SECONDS=interval)
                self.assertEqual(result.returncode, 2, self.diagnostic(result))
                self.assertIn("LP_BEAT_SECONDS must be a positive integer", result.stderr)
                self.assertNotIn("CHILD_MUST_NOT_RUN", result.stdout)
                self.assertNotIn("invalid: started", result.stdout)

    def test_leading_zero_positive_interval_is_decimal_not_octal(self):
        # Observe the requested delay instead of actually waiting eight seconds.
        # Only the ticker sees this function; the child is a fresh Bash process.
        result = self.run_shell(r'''
            sleep() { printf '%s\n' "$1" >> interval.trace; return 1; }
            run_beat decimal bash -c 'sleep 0.15; printf "CHILD_DONE\n"'
        ''', LP_BEAT_SECONDS="08")
        self.assertEqual(result.returncode, 0, self.diagnostic(result))
        delays = (self.root / "interval.trace").read_text().splitlines()
        self.assertEqual(len(delays), 1, delays)
        self.assertEqual(int(delays[0], 10), 8, delays)
        self.assertIn("CHILD_DONE", result.stdout)
        self.assertNotIn("value too great for base", result.stderr)
        self.assertNotIn("invalid", result.stderr.lower())

    def test_sourcing_and_run_beat_preserve_entry_exit_and_term_traps(self):
        result = self.run_shell(r'''
            trap 'entry_status=$?; printf "ENTRY_EXIT:%s\n" "$entry_status" >> traps.trace; exit "$entry_status"' EXIT
            trap 'printf "ENTRY_TERM\n" >> traps.trace' TERM
            trap -p EXIT TERM > traps.before
            source fixture/log.sh
            source fixture/log.sh
            trap -p EXIT TERM > traps.after-source
            run_beat traps bash -c 'exit 0'
            if run_beat failed-traps bash -c 'exit 19'; then
              exit 90
            else
              test "$?" = 19
            fi
            trap -p EXIT TERM > traps.after-run
            kill -s TERM "$$"
            printf 'AFTER_TERM\n'
            exit 37
        ''', source=False)
        self.assertEqual(result.returncode, 37, self.diagnostic(result))
        before = (self.root / "traps.before").read_text()
        self.assertEqual((self.root / "traps.after-source").read_text(), before)
        self.assertEqual((self.root / "traps.after-run").read_text(), before)
        self.assertEqual((self.root / "traps.trace").read_text().splitlines(),
                         ["ENTRY_TERM", "ENTRY_EXIT:37"])
        self.assertIn("AFTER_TERM\n", result.stdout)

    def test_failure_summary_is_created_once_and_groups_are_balanced(self):
        result = self.run_shell(r'''
            export GITHUB_STEP_SUMMARY="$PWD/summary.md"
            test ! -e "$GITHUB_STEP_SUMMARY"
            trap 'entry_status=$?; trap - EXIT; lp_finish "$entry_status"; lp_finish "$entry_status"; exit "$entry_status"' EXIT
            group_start 'prepare'
            run_beat prepare bash -c 'exit 0'
            group_end
            group_start 'compile | mock'
            run_beat compile bash -c 'printf "mock compiler failure\n" >&2; exit 37'
            printf 'UNREACHABLE\n'
        ''', GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 37, self.diagnostic(result))
        self.assertTrue(self.summary.is_file(), self.diagnostic(result))
        summary = self.summary.read_text()
        self.assertEqual(summary.count("### Build stages"), 1, summary)
        self.assertEqual(summary.count("failed (exit 37)"), 1, summary)
        self.assertEqual(len(re.findall(r"^\| prepare \| \d+s \| ok \|$", summary, re.M)), 1)
        self.assertEqual(len(re.findall(
            r"^\| compile \\\| mock \| \d+s \| failed \(exit 37\) \|$", summary, re.M)), 1)
        self.assert_groups_balanced(result.stdout, expected=2)
        self.assertNotIn("### Build stages", result.stdout)
        self.assertNotIn("UNREACHABLE", result.stdout)
        self.assertEqual(result.stderr.count("failed (exit 37): compile | mock"), 1)
        self.assertLess(result.stdout.index("::endgroup::"),
                        result.stdout.index("ok: prepare"))

    def test_unwritable_summary_falls_back_once_without_changing_failure_status(self):
        # A missing parent fails even for an administrator/root test account.
        result = self.run_shell(r'''
            export GITHUB_STEP_SUMMARY="$PWD/missing parent/summary.md"
            test ! -e "$GITHUB_STEP_SUMMARY"
            trap 'entry_status=$?; trap - EXIT; lp_finish "$entry_status"; lp_finish "$entry_status"; exit "$entry_status"' EXIT
            group_start 'broken mock'
            run_beat broken bash -c 'exit 23'
        ''', GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 23, self.diagnostic(result))
        self.assertFalse((self.root / "missing parent" / "summary.md").exists())
        self.assertEqual(result.stdout.count("### Build stages"), 1)
        self.assertEqual(result.stdout.count("failed (exit 23)"), 1)
        self.assertEqual(result.stderr.count("could not write the step summary"), 1)
        self.assert_groups_balanced(result.stdout, expected=1)

    def test_no_color_and_dumb_terminal_disable_ansi_with_ci_enabled(self):
        for settings in ({"NO_COLOR": "1", "TERM": "xterm"},
                         {"NO_COLOR": None, "TERM": "dumb"}):
            with self.subTest(**settings):
                result = self.run_shell("log plain\nwarn warning",
                                        GITHUB_ACTIONS="true", **settings)
                self.assertEqual(result.returncode, 0, self.diagnostic(result))
                self.assertEqual(result.stdout, "==> plain\n")
                self.assertEqual(result.stderr, "!!! warning\n")
                self.assertNotIn("\x1b", result.stdout + result.stderr)

    def test_color_positive_control_prevents_a_vacuous_no_color_test(self):
        result = self.run_shell("log colored\nwarn warning", NO_COLOR=None,
                                TERM="xterm", GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 0, self.diagnostic(result))
        self.assertIn("\x1b[", result.stdout)
        self.assertIn("\x1b[", result.stderr)

    @unittest.skipUnless(POSIX_SIGNALS,
                         "requires POSIX kill/killpg semantics; Windows Git Bash "
                         "does not prove Unix process-group cancellation")
    def test_posix_term_cancels_command_group_and_live_descendants(self):
        process = self.start_shell(r'''
            real_sleep=$(command -v sleep)
            sleep() {
              printf '%s\n' "$BASHPID" > ticker.pid
              # Exercise the production ticker's group settings unchanged.
              "$real_sleep" "$@" &
              printf '%s\n' "$!" > ticker-sleep.pid
              wait "$!"
            }
            run_beat cancel bash -c '
              printf "%s\n" "$PPID" > runner.pid
              printf "%s\n" "$$" > command.pid
              bash -c '\''printf "%s\n" "$$" > descendant.pid; exec sleep 4'\'' &
              descendant=$!
              trap '\''kill -TERM "$descendant" 2>/dev/null || true; wait "$descendant" 2>/dev/null || true; exit 143'\'' TERM
              wait "$descendant"
            '
        ''')
        files = [self.root / name for name in
                 ("runner.pid", "command.pid", "descendant.pid",
                  "ticker.pid", "ticker-sleep.pid")]
        deadline = time.monotonic() + 2
        while not all(path.exists() and path.stat().st_size for path in files):
            self.assertIsNone(process.poll(), "mock exited before PID handshake")
            if time.monotonic() >= deadline:
                self.fail("mock child/descendant did not complete PID handshake")
            time.sleep(0.02)
        pids = [int(path.read_text()) for path in files]

        def alive(pid):
            # Zombies have exited and cannot execute; some container PID 1s reap
            # them slowly. This assertion concerns surviving executable children.
            stat = Path(f"/proc/{pid}/stat")
            try:
                if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return False
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        def remove_leftovers():
            # Child groups are outside the launcher's group under Bash set -m.
            # Never target this Python test runner's own process group.
            for leader in (pids[1], pids[3]):
                if alive(leader):
                    try:
                        os.killpg(os.getpgid(leader), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for pid in pids:
                if alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        self.addCleanup(remove_leftovers)
        self.assertNotEqual(os.getpgid(pids[1]), os.getpgid(pids[0]),
                            "mock command must own a separate process group")
        self.assertEqual(os.getpgid(pids[2]), os.getpgid(pids[1]),
                         "mock descendant must belong to the command's group")
        self.assertNotEqual(os.getpgid(pids[3]), os.getpgid(pids[0]),
                            "ticker must own a separate process group")
        self.assertEqual(os.getpgid(pids[4]), os.getpgid(pids[3]),
                         "ticker's sleep must belong to the ticker's group")
        started = time.monotonic()
        os.kill(pids[0], signal.SIGTERM)
        result = self.collect(process, timeout=2.5)
        self.assertEqual(result.returncode, 143, self.diagnostic(result))
        self.assertLess(time.monotonic() - started, 2,
                        "cancellation waited for the mock command or ticker")
        deadline = time.monotonic() + 1
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([pid for pid in pids if alive(pid)], [],
                         "runner, command, descendant, ticker, or sleep survived cancellation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
