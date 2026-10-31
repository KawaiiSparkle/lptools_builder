#!/usr/bin/env python3
"""Isolated host orchestration tests: no downloads, AOSP builds, or publishing.

Run: python3 tests/test-host-build.py
Requires Python 3, Git, and Bash/coreutils (including Git Bash on Windows).
The two build scripts and log/aosp-meta/make-manifest helpers are copied intact.
Only AOSP/envsetup, repo, uname, file, check_projects and zip are fixtures; Git
commits/apply, metadata, hashes, tar/gzip and Python ZIP archives are real.
"""

import hashlib
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
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")
PROJECTS = (
    ("system/extras", "0001-system-extras-partition_tools-windows.patch"),
    ("system/core", "0002-system-core-liblp-libsparse-windows.patch"),
    ("external/e2fsprogs", "0003-external-e2fsprogs-ext2simg-windows.patch"),
)
BASE_TEXT = "context before\nfeature=posix\ncontext after\n"
PATCHED_TEXT = BASE_TEXT.replace(
    "feature=posix", 'feature=windows\ncompile_multilib: "64", // lptools Windows bitness')


class HostBuildFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BASH or not GIT:
            raise unittest.SkipTest("Bash and real Git are required (Git Bash on Windows)")
        env = os.environ.copy()
        for key in ("BASH_ENV", "ENV"):
            env.pop(key, None)
        try:
            result = subprocess.run(
                [BASH, "--noprofile", "--norc", "-c",
                 "set -eu; for tool in git tar gzip patch chmod mktemp tr sed awk "
                 "grep head tail cat cp mv rm mkdir sleep date df cut; do "
                 "command -v \"$tool\" >/dev/null || exit 88; done; "
                 "test -n \"$BASH_VERSION\""],
                env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise unittest.SkipTest(f"usable Bash/coreutils unavailable: {exc}")
        if result.returncode:
            raise unittest.SkipTest(
                f"Bash/coreutils probe failed ({result.returncode}): {result.stdout}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="host build mock ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "source tree"
        self.out = self.root / "dist tree"
        self.output = self.trace = self.summary = ""
        (self.root / "tmp").mkdir()
        self.put("gitconfig", "")
        self.env = os.environ.copy()
        # Neither caller build settings, injected shell startup, nor Git user
        # configuration may alter the fixtures or touch the caller's projects.
        for key in list(self.env):
            if key.startswith(("GIT_", "LPTOOLS_", "MOCK_")) or key in (
                    "BASH_ENV", "ENV", "AOSP_REF", "JOBS", "OUT", "OUT_DIR",
                    "ANDROID_HOST_OUT", "HOST_CROSS_OS", "HOST_CROSS_ARCH",
                    "TARGET_BUILD_UNBUNDLED", "ALLOW_MISSING_DEPENDENCIES",
                    "LP_NO_GROUPS", "__LP_LOG_SH", "SOURCE_DATE_EPOCH_ISO"):
                self.env.pop(key, None)
        self.env.update(GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=(self.root / "gitconfig").as_posix(),
                        GIT_TERMINAL_PROMPT="0")
        scripts = self.root / "fixture/scripts"
        scripts.mkdir(parents=True)
        for name in ("build-linux.sh", "build-windows.sh", "log.sh",
                     "aosp-meta.sh", "aosp-lunch.sh", "make-manifest.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        self.put("fixture/required-projects.txt",
                 "# Fixture projects only\nsystem/extras\nsystem/core\nexternal/e2fsprogs\n")
        self.put("source tree/.repo/manifest.xml",
                 '<manifest><default revision="mock-branch"/></manifest>\n')
        manifest_repo = self.work / ".repo/manifests"
        self.put("source tree/.repo/manifests/default.xml",
                 '<manifest><default revision="mock-branch"/></manifest>\n')
        self.init_repo(manifest_repo)
        self.git(manifest_repo, "tag", "fixture-manifest-tag")
        self.manifest_revision = self.git(manifest_repo, "rev-parse", "HEAD").strip()
        self.put("mock-bin/python3", "#!/usr/bin/env bash\nexec " +
                 shlex.quote(Path(sys.executable).as_posix()) + ' \"$@\"\n')
        self.put("mock-bin/uname", r'''
            #!/usr/bin/env bash
            case "${1:-}" in
              -s) printf 'Linux\n' ;;
              -m) printf '%s\n' "${MOCK_NATIVE_ARCH:-x86_64}" ;;
              *) exit 90 ;;
            esac
        ''')
        self.put("mock-bin/repo", r'''
            #!/usr/bin/env bash
            set -eu
            case "${1:-}" in
              --version) printf 'repo version fixture-1\n' ;;
              init)
                printf 'repo-init\t%s' "$PWD" >> "$TRACE"
                printf '\t%s' "$@" >> "$TRACE"; printf '\n' >> "$TRACE"
                ref=
                while [ $# -gt 0 ]; do
                  if [ "$1" = -b ]; then ref=$2; break; fi
                  shift
                done
                [ -n "$ref" ] || exit 91
                mkdir -p .repo
                printf '%s\n' "$ref" > .repo/requested-ref
                if [ -n "${MOCK_MANIFEST_XML:-}" ]; then
                  printf '%s\n' "$MOCK_MANIFEST_XML" > .repo/manifest.xml
                else
                  printf '<manifest><default revision="%s"/></manifest>\n' "$ref" > .repo/manifest.xml
                fi
                ;;
              sync)
                printf 'repo-sync\t%s' "$PWD" >> "$TRACE"
                printf '\t%s' "$@" >> "$TRACE"; printf '\n' >> "$TRACE"
                [ -f .repo/requested-ref ] || exit 92
                ;;
              manifest)
                shift; output=
                while [ $# -gt 0 ]; do
                  if [ "$1" = -o ]; then output=$2; break; fi
                  shift
                done
                case "$output" in /*) ;; *) exit 93 ;; esac
                printf 'manifest\t%s\t%s\n' "$PWD" "$output" >> "$TRACE"
                {
                  printf '<manifest>\n'
                  for project in system/extras system/core external/e2fsprogs; do
                    printf '  <project name="%s" revision="0123456789012345678901234567890123456789"/>\n' "$project"
                  done
                  printf '</manifest>\n'
                } > "$output"
                ;;
              *) printf 'forbidden repo command: %s\n' "$*" >&2; exit 94 ;;
            esac
        ''')
        self.put("mock-bin/file", r'''
            #!/usr/bin/env bash
            printf 'file\t%s\n' "$*" >> "$TRACE"
            if [ "${1:-}" = -b ]; then
              printf '%s\n' "$MOCK_FILE_DESCRIPTION"
            else
              for arg in "$@"; do printf '%s: %s\n' "$arg" "$MOCK_FILE_DESCRIPTION"; done
            fi
        ''')
        # A real Python ZIP keeps the test portable when Git Bash has no zip.
        self.put("mock-bin/zip", r'''
            #!/usr/bin/env bash
            printf 'zip\t%s\n' "$PWD" >> "$TRACE"
            exec python3 "$MOCK_ROOT/helpers/zip.py" "$@"
        ''')
        self.put("helpers/zip.py", '''
            import sys
            from pathlib import Path
            import zipfile
            paths = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
            if len(paths) != 2:
                raise SystemExit(95)
            destination, source = paths
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(Path(source).rglob("*")):
                    if path.is_file():
                        archive.write(path, path.as_posix())
        ''')
        self.put("fixture/tools/check_projects.py", r'''
            import argparse
            import os
            from pathlib import Path
            parser = argparse.ArgumentParser()
            parser.add_argument("--repo-root", required=True)
            parser.add_argument("--list", required=True)
            parser.add_argument("--print-projects", action="store_true")
            args = parser.parse_args()
            work = Path(args.repo_root)
            assert work.is_absolute(), args.repo_root
            assert Path(args.list).is_file(), args.list
            requested = (work / ".repo/requested-ref").read_text().strip()
            assert requested == os.environ["MOCK_EXPECT_REF"], requested
            with (work.parent / "trace").open("a", encoding="utf-8") as stream:
                stream.write("check-projects\t" + requested + "\n")
            if args.print_projects:
                print("system/extras\nsystem/core\nexternal/e2fsprogs")
        ''')
        for name in ("sudo", "curl", "wget", "ssh", "make", "ninja", "m", "gh"):
            self.put("mock-bin/" + name, r'''
                #!/usr/bin/env bash
                printf 'forbidden\t%s\n' "$0 $*" >> "$TRACE"
                printf 'forbidden external command: %s\n' "$0" >&2
                exit 96
            ''')
        self.put("source tree/build/envsetup.sh", r'''
            printf 'source\t%s\t%s\n' "$BASHPID" "$PWD" >> "$TRACE"
            MOCK_SETUP_PID=$BASHPID
            [ "${MOCK_SOURCE_RC:-0}" = 0 ] || return "$MOCK_SOURCE_RC"
            printf 'fixture setup entered\n'
            sleep "${MOCK_SETUP_DELAY:-0}"
            printf 'fixture setup completed\n'
            lunch() {
              [ "${TARGET_BUILD_UNBUNDLED:-}" = true ] || return 93
              [ "${ALLOW_MISSING_DEPENDENCIES:-}" = true ] || return 94
              printf 'lunch\t%s\t%s\n' "$BASHPID" "$1" >> "$TRACE"
              sleep "${MOCK_LUNCH_DELAY:-0}"
              [ "${MOCK_LUNCH_RC:-0}" = 0 ] || return "$MOCK_LUNCH_RC"
              MOCK_LUNCH_PID=$BASHPID
              MOCK_COMBO=$1
              if [ "${MOCK_MODERN:-0}" = 1 ]; then
                case "$1" in *-trunk_staging-eng) ;; *) return 21 ;; esac
              fi
              export ANDROID_HOST_OUT="selected host artifacts/native host"
              if [ "${MOCK_ABSOLUTE_HOST:-0}" = 1 ]; then
                export ANDROID_HOST_OUT="$PWD/absolute host artifacts/native host"
              fi
              if [ "${MOCK_ABSOLUTE_OUT:-0}" = 1 ]; then
                export OUT_DIR="$PWD/absolute build artifacts"
              fi
              # Deliberately poison the variable also used by the outer packager.
              export OUT="$PWD/lunch polluted OUT"
            }
            m() {
              [ "${TARGET_BUILD_UNBUNDLED:-}" = true ] || return 93
              [ "${ALLOW_MISSING_DEPENDENCIES:-}" = true ] || return 94
              [ "${1:-}" = --soong-only ] || return 95
              shift
              printf 'm\t%s\t%s\t%s\t%s' "$BASHPID" "${HOST_CROSS_OS:-}" "${HOST_CROSS_ARCH:-}" "$PWD" >> "$TRACE"
              printf '\t%s' "$@" >> "$TRACE"; printf '\n' >> "$TRACE"
              [ "$MOCK_SETUP_PID" = "$BASHPID" ] && [ "$MOCK_LUNCH_PID" = "$BASHPID" ] || return 97
              [ "${MOCK_BUILD_RC:-0}" = 0 ] || return "$MOCK_BUILD_RC"
              if [ "$MOCK_KIND" = windows ]; then
                case "${2:-}" in
                  */lpmake.exe)
                    [ "${MOCK_PATH_GOALS_RC:-0}" = 0 ] || return "$MOCK_PATH_GOALS_RC" ;;
                esac
                local build_out="${OUT_DIR:-out}"
                case "$build_out" in /*) ;; *) build_out="$PWD/$build_out" ;; esac
                local bin="$build_out/soong/host/windows-x86/bin"
                local bits=64
                [ "$HOST_ARCH" != x86 ] || bits=32
                grep -q 'compile_multilib: "'"$bits"'", // lptools Windows bitness' system/extras/subject.txt || return 98
                mkdir -p "$bin"
                local module
                for module in lpmake lpdumps lpunpack lpadd simg2img img2simg append2simg ext2simg; do
                  printf '%s\n' "$MOCK_COMBO" > "$bin/$module.exe"
                done
                if [ "${MOCK_USER_CHANGES:-0}" = 1 ]; then
                  printf 'user edit made during build\n' > system/core/user.txt
                  printf 'user untracked data\n' > system/core/generated-user.txt
                fi
              else
                local build_out="${OUT_DIR:-out}"
                local arch="${MOCK_NATIVE_ARCH:-x86_64}"
                case "$arch" in x86_64) arch=x86 ;; aarch64) arch=arm64 ;; esac
                local host_out="$build_out/soong/host/linux-$arch"
                case "$host_out" in /*) ;; *) host_out="$PWD/$host_out" ;; esac
                mkdir -p "$host_out/bin" "$host_out/lib64/nested" "$host_out/lib"
                printf 'fixture runtime\n' > "$host_out/lib64/libfixture.so"
                printf 'transitive runtime\n' > "$host_out/lib64/nested/libtransitive.so"
                printf 'secondary runtime\n' > "$host_out/lib/libsecondary.so"
                local module
                for module in lpmake lpdump lpunpack simg2img img2simg lpadd lpflash; do
                  printf '#!/usr/bin/env bash\n# %s\nexit 0\n' "$MOCK_COMBO" > "$host_out/bin/$module"
                done
              fi
            }
        ''')

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(textwrap.dedent(content).lstrip("\n"))
        path.chmod(0o755)
        return path

    def git(self, project, *args):
        result = subprocess.run(
            [GIT, "-c", "core.autocrlf=false", "-c", "core.filemode=false",
             "-C", str(project), *args],
            env=self.env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 0,
                         f"git {args}: {result.stdout}\n{result.stderr}")
        return result.stdout

    def init_repo(self, project):
        self.git(project, "init", "-q")
        self.git(project, "add", ".")
        self.git(project, "-c", "user.name=Host Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "-qm", "fixture base")

    def rows(self, event):
        return [line.split("\t")[1:] for line in self.trace.splitlines()
                if line.startswith(event + "\t")]

    def run_build(self, kind, *extra, sync=False, **settings):
        for name in ("trace", "summary.md"):
            (self.root / name).unlink(missing_ok=True)
        env = self.env.copy()
        env.update(GITHUB_ACTIONS="true", NO_COLOR="1", LP_TS="0",
                   LP_BEAT_SECONDS="1", OUT_DIR="custom build artifacts",
                   AOSP_REF="android-15.0.0_r1", MOCK_KIND=kind,
                   MOCK_FILE_DESCRIPTION=("PE32+ executable (console) x86-64, for MS Windows"
                                          if kind == "windows" else
                                          "ELF 64-bit LSB executable, x86-64"))
        env.update({key: str(value) for key, value in settings.items()})
        # Form POSIX absolute paths inside Bash; native Python on Windows would
        # otherwise pass drive paths that case /*) in the scripts cannot parse.
        launcher = (
            'kind=$1; shift; export PATH="$PWD/mock-bin:$PATH" '
            'TRACE="$PWD/trace" MOCK_ROOT="$PWD" TMPDIR="$PWD/tmp" '
            'GITHUB_STEP_SUMMARY="$PWD/summary.md"; '
            'exec bash "fixture/scripts/build-$kind.sh" "$@"')
        args = [BASH, "--noprofile", "--norc", "-c", launcher, "host-mock", kind,
                "--work", "source tree", "--out", "dist tree", "--jobs", "2"]
        if not sync:
            args.append("--skip-sync")
        if kind == "linux":
            args.append("--no-smoke")
        result = subprocess.run(
            args + list(extra), cwd=self.root, env=env, text=True,
            encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=40)
        self.output = result.stdout
        self.trace = ((self.root / "trace").read_text(encoding="utf-8")
                      if (self.root / "trace").exists() else "")
        self.summary = ((self.root / "summary.md").read_text(encoding="utf-8")
                        if (self.root / "summary.md").exists() else "")
        return result.returncode

    def assert_housekeeping(self, summary=True):
        self.assertEqual(list((self.root / "tmp").iterdir()), [], self.output)
        depth = 0
        for line in self.output.splitlines():
            if line.startswith("::group::"):
                depth += 1
            elif line == "::endgroup::":
                depth -= 1
                self.assertGreaterEqual(depth, 0, self.output)
        self.assertEqual(depth, 0, self.output)
        self.assertLessEqual(self.summary.count("### Build stages"), 1, self.output)
        if summary:
            self.assertEqual(self.summary.count("### Build stages"), 1, self.output)
        self.assertNotIn("forbidden\t", self.trace, self.trace)

    def assert_failed(self, rc):
        self.assertIn(f"failed (exit {rc})", self.summary, self.output)
        self.assertFalse(list(self.out.glob("*.tar.gz")), self.output)
        self.assertFalse(list(self.out.glob("*.zip")), self.output)
        self.assert_housekeeping()

    def assert_one_build_shell(self):
        source = self.rows("source")
        lunches = self.rows("lunch")
        compiles = self.rows("m")
        self.assertEqual(len(source), 1, self.trace)
        self.assertTrue(lunches, self.trace)
        self.assertTrue(compiles, self.trace)
        self.assertEqual({row[0] for row in source + lunches + compiles},
                         {source[0][0]}, self.trace)

    def assert_package(self, platform, product="module_arm64-trunk_staging-eng"):
        bundle_name = "lptools-" + platform
        bundle = self.out / bundle_name
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["platform"], platform)
        self.assertEqual(manifest["build_product"], product)
        self.assertEqual(manifest["aosp_branch"], "mock-branch")
        self.assertEqual(manifest["aosp_tag"], "fixture-manifest-tag")
        self.assertEqual(manifest["aosp_manifest_revision"], self.manifest_revision)
        self.assertEqual(manifest["aosp_pinned_manifest"]["projects"], 3)
        self.assertEqual(len(self.rows("manifest")), 1, self.trace)
        for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((bundle / relative).read_bytes()).hexdigest(), digest)
        if platform.startswith("windows-"):
            archive_path = self.out / (bundle_name + ".zip")
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIn(bundle_name + "/manifest.json", archive.namelist())
                self.assertIn(bundle_name + "/bin/lpdump.exe", archive.namelist())
                self.assertNotIn(bundle_name + "/bin/lpdumps.exe", archive.namelist())
            self.assertEqual(manifest["kind"], "windows")
        else:
            archive_path = self.out / (bundle_name + ".tar.gz")
            with tarfile.open(archive_path) as archive:
                self.assertIn(bundle_name + "/manifest.json", archive.getnames())
                self.assertIn(bundle_name + "/bin/lpmake", archive.getnames())
                for relative, payload in {
                    "lib64/libfixture.so": b"fixture runtime\n",
                    "lib64/nested/libtransitive.so": b"transitive runtime\n",
                    "lib/libsecondary.so": b"secondary runtime\n",
                }.items():
                    member = bundle_name + "/" + relative
                    self.assertIn(member, archive.getnames())
                    self.assertEqual(archive.extractfile(member).read(), payload)
                    self.assertEqual((bundle / relative).read_bytes(), payload)
                    self.assertIn(relative, manifest["files"])
                    self.assertEqual(manifest["files"][relative]["sha256"],
                                     hashlib.sha256(payload).hexdigest())
            self.assertEqual(manifest["kind"], "host")
        checksum = Path(str(archive_path) + ".sha256").read_text().split()[0]
        self.assertEqual(checksum, hashlib.sha256(archive_path.read_bytes()).hexdigest())
        self.assertFalse((self.work / "lunch polluted OUT").exists(), self.output)
        self.assertFalse(self.rows("repo-init"), self.trace)
        self.assertFalse(self.rows("repo-sync"), self.trace)
        self.assert_housekeeping()
        return manifest


class LinuxHostBuildTest(HostBuildFixture):
    def test_sync_uses_real_manifest_names_and_rejects_invented_alias(self):
        shutil.copyfile(ROOT / "tools/check_projects.py",
                        self.root / "fixture/tools/check_projects.py")
        self.put("fixture/required-projects.txt",
                 "platform/build\nbuild/make\nplatform/build\n")
        manifest = ('<manifest><project name="platform/build" path="build/make"/>'
                    '</manifest>')
        self.assertEqual(self.run_build("linux", "--sync-only", sync=True,
                                        MOCK_MANIFEST_XML=manifest), 0, self.output)
        self.assertEqual(self.rows("repo-sync")[0][-1], "platform/build", self.trace)
        self.assertEqual(self.rows("repo-sync")[0].count("platform/build"), 1)
        self.assertNotIn("build/make", self.rows("repo-sync")[0])
        self.assert_housekeeping()

        self.put("fixture/required-projects.txt", "platform/build/make\n")
        self.assertEqual(self.run_build("linux", "--sync-only", sync=True,
                                        MOCK_MANIFEST_XML=manifest), 1, self.output)
        self.assertIn("project not found in manifest: platform/build/make", self.output)
        self.assertFalse(self.rows("repo-sync"), self.trace)
        self.assert_housekeeping()

    def test_skip_sync_spaces_soong_out_one_shell_and_setup_heartbeat(self):
        self.assertEqual(self.run_build("linux", MOCK_SETUP_DELAY=3), 0, self.output)
        self.assert_one_build_shell()
        self.assertIn("AOSP host build still running", self.output)
        self.assertLess(self.output.index("still running"),
                        self.output.index("fixture setup completed"), self.output)
        manifest = self.assert_package("linux-x86_64")
        self.assertEqual(manifest["host_arch"], "x86_64")
        expected = self.work / "custom build artifacts/soong/host/linux-x86/bin/lpmake"
        self.assertTrue(expected.is_file(), self.output)
        self.assertEqual((self.out / "lptools-linux-x86_64/bin/lpmake").read_bytes(),
                         expected.read_bytes())
        self.assertFalse((self.work / "selected host artifacts/native host/bin").exists())

    def test_absolute_soong_out_and_normalized_arm64(self):
        self.assertEqual(self.run_build(
            "linux", "--arch", "aarch64", MOCK_NATIVE_ARCH="aarch64",
            MOCK_ABSOLUTE_OUT=1, MOCK_FILE_DESCRIPTION="ELF 64-bit LSB executable, aarch64"),
            0, self.output)
        self.assert_package("linux-arm64")
        self.assertTrue((self.work / "absolute build artifacts/soong/host/linux-arm64/bin/lpmake").is_file())

    def test_envsetup_failure_preserves_status(self):
        self.assertEqual(self.run_build("linux", MOCK_SOURCE_RC=17), 17, self.output)
        self.assertFalse(self.rows("lunch"), self.trace)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_failed(17)

    def test_lunch_failure_preserves_status(self):
        self.assertEqual(self.run_build("linux", MOCK_LUNCH_RC=29), 29, self.output)
        self.assertGreater(len(self.rows("lunch")), 1, self.trace)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_failed(29)

    def test_build_failure_preserves_status(self):
        self.assertEqual(self.run_build("linux", MOCK_BUILD_RC=37), 37, self.output)
        self.assertEqual(len(self.rows("m")), 1, self.trace)
        self.assert_failed(37)

    def test_arch_mismatch_rejected_before_initialization(self):
        self.assertEqual(self.run_build("linux", "--arch", "arm64"), 1, self.output)
        self.assertIn("does not match host x86_64", self.output)
        self.assertFalse(self.rows("source"), self.trace)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_housekeeping(summary=False)

    def test_file_architecture_mismatch_rejected_before_packaging(self):
        self.assertEqual(self.run_build(
            "linux", MOCK_FILE_DESCRIPTION="ELF 64-bit LSB executable, aarch64"), 1, self.output)
        self.assertIn("architecture does not match linux-x86_64", self.output)
        self.assertFalse(self.rows("manifest"), self.trace)
        self.assert_failed(1)

    def test_reused_repo_sync_only_refreshes_each_requested_ref(self):
        for ref in ("fixture-old-ref", "fixture-new-ref"):
            with self.subTest(ref=ref):
                self.assertEqual(self.run_build(
                    "linux", "--sync-only", "--ref", ref, sync=True,
                    MOCK_EXPECT_REF=ref), 0, self.output)
                self.assertEqual((self.work / ".repo/requested-ref").read_text().strip(), ref)
                self.assertEqual(len(self.rows("repo-init")), 1, self.trace)
                self.assertIn(ref, self.rows("repo-init")[0])
                self.assertEqual(self.rows("check-projects"), [[ref]], self.trace)
                self.assertEqual(len(self.rows("repo-sync")), 1, self.trace)
                self.assertLess(self.trace.index("repo-init\t"), self.trace.index("check-projects\t"))
                self.assertLess(self.trace.index("check-projects\t"), self.trace.index("repo-sync\t"))
                self.assertFalse(self.rows("source"), self.trace)
                self.assertFalse(self.rows("m"), self.trace)
                self.assert_housekeeping()


class WindowsHostBuildTest(HostBuildFixture):
    def setUp(self):
        super().setUp()
        for project, patch_name in PROJECTS:
            self.put("source tree/" + project + "/subject.txt", BASE_TEXT)
            self.put("source tree/" + project + "/user.txt", "original user data\n")
            path = self.work / project
            self.init_repo(path)
            self.put("source tree/" + project + "/subject.txt", PATCHED_TEXT)
            patch = self.git(path, "diff", "--binary", "--", "subject.txt")
            self.assertIn("+feature=windows", patch)
            patch_path = self.put("fixture/patches/" + patch_name, patch)
            self.git(path, "apply", "--reverse", str(patch_path))
            self.assertEqual(self.git(path, "status", "--porcelain"), "")

    def snapshots(self):
        state = {}
        for project, _ in PROJECTS:
            path = self.work / project
            state[project] = {
                "files": {item.relative_to(path).as_posix(): item.read_bytes()
                          for item in path.rglob("*")
                          if item.is_file() and ".git" not in item.relative_to(path).parts},
                "index": self.git(path, "ls-files", "--stage"),
                "staged_diff": self.git(path, "diff", "--cached", "--binary"),
                "head": self.git(path, "rev-parse", "HEAD"),
            }
        return state

    def assert_patch_contents(self, patched):
        for project, _ in PROJECTS:
            self.assertEqual((self.work / project / "subject.txt").read_text(),
                             PATCHED_TEXT if patched else BASE_TEXT, project)
            self.assertEqual(self.git(self.work / project, "diff", "--cached"), "", project)

    def test_check_patches_is_read_only_and_does_not_initialize_or_build(self):
        self.put("source tree/system/core/user.txt", "staged user data\n")
        self.git(self.work / "system/core", "add", "user.txt")
        self.put("source tree/external/e2fsprogs/extra.txt", "untracked user data\n")
        before = self.snapshots()
        indexes = {project: (self.work / project / ".git/index").read_bytes()
                   for project, _ in PROJECTS}
        self.assertEqual(self.run_build("windows", "--check-patches"), 0, self.output)
        self.assertEqual(self.snapshots(), before)
        for project, _ in PROJECTS:
            self.assertEqual((self.work / project / ".git/index").read_bytes(), indexes[project])
        self.assertEqual(self.output.count("(git apply --check)"), 3, self.output)
        self.assertFalse(self.rows("source"), self.trace)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_housekeeping(summary=False)

    def test_build_success_fallback_paths_heartbeat_and_patch_only_cleanup(self):
        self.assertEqual(self.run_build(
            "windows", MOCK_SETUP_DELAY=3, MOCK_PATH_GOALS_RC=41,
            MOCK_USER_CHANGES=1, MOCK_MODERN=1), 0, self.output)
        self.assert_one_build_shell()
        self.assertIn("AOSP Windows build still running", self.output)
        self.assertLess(self.output.index("still running"),
                        self.output.index("fixture setup completed"), self.output)
        calls = self.rows("m")
        self.assertEqual(len(calls), 7, self.trace)
        first = calls[0]
        self.assertEqual(first[4], "-j2", self.trace)
        for goal, tool in zip(first[5:], ("lpmake", "lpdumps", "lpunpack")):
            self.assertEqual(
                goal, f"custom build artifacts/soong/host/windows-x86/bin/{tool}.exe")
        self.assertEqual(calls[1][1:3], ["windows", "x86_64"], self.trace)
        self.assertEqual(calls[1][4:], ["-j2", "lpmake", "lpdumps", "lpunpack"], self.trace)
        self.assertIn("retrying with module names", self.output)
        self.assert_patch_contents(False)
        self.assertEqual((self.work / "system/core/user.txt").read_text(),
                         "user edit made during build\n")
        self.assertEqual((self.work / "system/core/generated-user.txt").read_text(),
                         "user untracked data\n")
        self.assert_package("windows-x86_64")

    def test_absolute_out_dir_and_x86_output_goals(self):
        self.assertEqual(self.run_build(
            "windows", "--arch", "i686", MOCK_ABSOLUTE_OUT=1,
            MOCK_FILE_DESCRIPTION="PE32 executable (console) Intel 80386, for MS Windows"),
            0, self.output)
        goals = self.rows("m")[0][5:]
        self.assertEqual(len(goals), 3, self.trace)
        for goal in goals:
            self.assertIn("/source tree/absolute build artifacts/soong/host/windows-x86/bin/", goal)
        self.assert_patch_contents(False)
        self.assert_package("windows-x86")

    def assert_preflight_preserves(self, change):
        # The last project is dirty: no earlier project may receive a patch.
        project = self.work / PROJECTS[-1][0]
        if change == "untracked":
            self.put("source tree/external/e2fsprogs/extra.txt", "user untracked data\n")
        else:
            self.put("source tree/external/e2fsprogs/user.txt", "user changed data\n")
            if change == "staged":
                self.git(project, "add", "user.txt")
        before = self.snapshots()
        self.assertEqual(self.run_build("windows"), 1, self.output)
        self.assertIn("external/e2fsprogs has local changes", self.output)
        self.assertEqual(self.snapshots(), before)
        self.assertFalse(self.rows("source"), self.trace)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_failed(1)

    def test_dirty_last_project_preflight_preserves_all_projects(self):
        self.assert_preflight_preserves("dirty")

    def test_staged_last_project_preflight_preserves_all_projects(self):
        self.assert_preflight_preserves("staged")

    def test_untracked_last_project_preflight_preserves_all_projects(self):
        self.assert_preflight_preserves("untracked")

    def test_envsetup_failure_preserves_status_and_patches(self):
        self.assertEqual(self.run_build("windows", MOCK_SOURCE_RC=17), 17, self.output)
        self.assertFalse(self.rows("lunch"), self.trace)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_patch_contents(True)
        self.assert_failed(17)

    def test_lunch_failure_preserves_status_and_patches(self):
        self.assertEqual(self.run_build("windows", MOCK_LUNCH_RC=29), 29, self.output)
        self.assertFalse(self.rows("m"), self.trace)
        self.assert_patch_contents(True)
        self.assert_failed(29)

    def test_build_failure_preserves_status_and_patches(self):
        self.assertEqual(self.run_build("windows", MOCK_BUILD_RC=37), 37, self.output)
        self.assertEqual(len(self.rows("m")), 2, self.trace)
        self.assert_patch_contents(True)
        self.assertIn("patches kept for inspection", self.output)
        self.assert_failed(37)

    def test_file_architecture_mismatch_keeps_patches_and_does_not_package(self):
        self.assertEqual(self.run_build(
            "windows", MOCK_FILE_DESCRIPTION="PE32 executable (console) Intel 80386"), 1, self.output)
        self.assertIn("architecture does not match windows-x86_64", self.output)
        self.assertFalse(self.rows("manifest"), self.trace)
        self.assert_patch_contents(True)
        self.assert_failed(1)

    def test_revert_only_preserves_staged_unstaged_and_untracked_user_files(self):
        for project, patch_name in PROJECTS:
            self.git(self.work / project, "apply", str(self.root / "fixture/patches" / patch_name))
        self.put("source tree/system/extras/user.txt", "staged extra data\n")
        self.git(self.work / "system/extras", "add", "user.txt")
        self.put("source tree/system/core/user.txt", "unstaged core data\n")
        self.put("source tree/external/e2fsprogs/extra.txt", "untracked e2fs data\n")
        expected = self.snapshots()
        for project, _ in PROJECTS:
            expected[project]["files"]["subject.txt"] = BASE_TEXT.encode()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.run_build("windows", "--revert-only"), 0, self.output)
                self.assertEqual(self.snapshots(), expected)
                self.assertFalse(self.rows("source"), self.trace)
                self.assertFalse(self.rows("m"), self.trace)
                self.assert_housekeeping(summary=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
