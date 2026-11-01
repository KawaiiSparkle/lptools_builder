#!/usr/bin/env python3
"""Provenance export checks for scripts/aosp-meta.sh.

The helper has to describe a *partially synced* AOSP tree truthfully: the
pipeline runs "repo sync <projects...>", so only a few dozen of the manifest's
projects are ever checked out. "repo manifest -r" cannot express that (it
resolves every project's HEAD and aborts on the first one that was never
synced), which is how bundles ended up with an empty aosp-manifest.xml,
"projects": 0 and a branch named "default" -- repo's synthetic local branch
name, not the branch that was actually fetched.

These tests drive the real helper with a stand-in "repo" that models the parts
of the real tool the helper depends on (a Python launcher, as the real repo
started out): flattening includes and .repo/local_manifests, refusing -r when a
project was never synced, and forall visiting only the projects that have a
checkout. Git is the real git, on real local repositories. No network, no repo
installation.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
GIT = shutil.which("git")

MOCK_REPO = r'''#!/usr/bin/env python3
"""Stand-in for the real repo launcher (only the parts the helper uses)."""
import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

TOPDIR = os.getcwd()
BASH = shutil.which("bash") or "/bin/sh"
REPO_DIR = os.path.join(TOPDIR, ".repo")
MANIFESTS = os.path.join(REPO_DIR, "manifests")


def nodes(path, depth=0):
    root = ET.parse(path).getroot()
    out = []
    for element in root:
        if element.tag == "include":
            name = element.get("name")
            if not name or depth > 8:
                continue
            out.extend(nodes(os.path.join(MANIFESTS, name), depth + 1))
        else:
            out.append(element)
    return out


def merged():
    """manifest.xml merged with its includes and local_manifests, like repo."""
    out = nodes(os.path.join(REPO_DIR, "manifest.xml"))
    localdir = os.path.join(REPO_DIR, "local_manifests")
    if os.path.isdir(localdir):
        for name in sorted(os.listdir(localdir)):
            if name.endswith(".xml"):
                out.extend(nodes(os.path.join(localdir, name)))
    root = ET.Element("manifest")
    seen_default = False
    for element in out:
        if element.tag == "default":
            if seen_default:
                continue
            seen_default = True
        root.append(element)
    return root


def project_path(element):
    return element.get("path") or element.get("name")


def cmd_manifest(args):
    # The real tool resolves every project's working-tree HEAD in this mode and
    # aborts the whole export once one of them was never synced.
    if any(a in ("-r", "--revision-as-HEAD") for a in args):
        sys.stderr.write("fatal: manifest: cannot resolve HEAD of unsynced project\n")
        return 1
    out = None
    for index, arg in enumerate(args):
        if arg in ("-o", "--output-file") and index + 1 < len(args):
            out = args[index + 1]
        elif arg.startswith("--output-file="):
            out = arg.split("=", 1)[1]
    tree = ET.ElementTree(merged())
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    if not out:
        tree.write(sys.stdout, encoding="unicode")
        return 0
    tree.write(out, encoding="unicode")
    return 0


def cmd_forall(args):
    ignore_missing = "--ignore-missing" in args
    command = None
    for index, arg in enumerate(args):
        if arg in ("-c", "--command") and index + 1 < len(args):
            command = args[index + 1:]
    if command is None:
        sys.stderr.write("fatal: forall: no command\n")
        return 1
    default_remote = None
    for element in merged():
        if element.tag == "default":
            default_remote = element.get("remote")
    rc = 0
    for element in merged():
        if element.tag != "project":
            continue
        path = project_path(element)
        worktree = os.path.join(TOPDIR, path)
        if not os.path.isdir(worktree):
            if ignore_missing:
                continue
            sys.stderr.write("skipping %s/\n" % path)
            return 1
        env = dict(os.environ)
        env["REPO_PATH"] = path
        env["REPO_PROJECT"] = element.get("name")
        env["REPO_REMOTE"] = element.get("remote") or default_remote or ""
        # repo executes a simple command and its remaining arguments directly.
        # The helper uses "sh <script>" so spaces in the script path stay intact.
        result = subprocess.run(command, cwd=worktree,
                                env=env, shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
        if result.returncode:
            rc = result.returncode
    return rc


def main(argv):
    if not argv:
        sys.stderr.write("usage: repo <subcommand>\n")
        return 2
    if argv[0] == "manifest":
        return cmd_manifest(argv[1:])
    if argv[0] == "forall":
        return cmd_forall(argv[1:])
    sys.stderr.write("fatal: unknown subcommand %s\n" % argv[0])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

MANIFEST_HEAD = """<manifest>
  <remote name="aosp" fetch=".." review="https://android-review.googlesource.com/"/>
  <default revision="refs/tags/android-15.0.0_r1" remote="aosp" sync-j="4"/>
"""

# Sourced and called exactly as the build scripts do, then the exported values
# are dumped and the helper's status becomes the shell's status.
DRIVER = """
set -uo pipefail
log() { printf 'LOG:%s\\n' "$*"; }
warn() { printf 'WARN:%s\\n' "$*" >&2; }
source "$LPTOOLS_TEST_ROOT/scripts/aosp-meta.sh"
if lptools_export_meta "$WORK" "$BUNDLE" "$REQUESTED"; then rc=0; else rc=$?; fi
printf 'RC:%s\\n' "$rc"
printf 'BRANCH:%s\\n' "${AOSP_BRANCH-}"
printf 'TAG:%s\\n' "${AOSP_TAG-}"
printf 'REV:%s\\n' "${AOSP_MANIFEST_REV-}"
printf 'PROJECTS:%s\\n' "${AOSP_PINNED_PROJECTS-}"
exit "$rc"
"""


@unittest.skipUnless(BASH and GIT, "Bash and git are required")
class AospMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lptools aosp meta "))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp / "checkout"
        self.bundle = self.tmp / "bundle"
        self.bundle.mkdir()
        (self.work / ".repo" / "manifests").mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        repo = self.bin / "repo"
        repo.write_text(MOCK_REPO, encoding="utf-8")
        repo.chmod(0o755)

    # -- fixtures -----------------------------------------------------------
    def write_manifest(self, body, name="default.xml"):
        """Write a manifest repo file plus the .repo/manifest.xml repo generates."""
        (self.work / ".repo" / "manifests" / name).write_text(body, encoding="utf-8")
        (self.work / ".repo" / "manifest.xml").write_text(
            '<manifest>\n  <include name="%s"/>\n</manifest>\n' % name,
            encoding="utf-8")

    def write_local_manifest(self, body, name="local.xml"):
        localdir = self.work / ".repo" / "local_manifests"
        localdir.mkdir(exist_ok=True)
        (localdir / name).write_text(body, encoding="utf-8")

    def manifest_checkout(self, merge=None):
        """Make .repo/manifests repo's real git checkout of the manifest repo."""
        manifests = self.work / ".repo" / "manifests"
        self.git(manifests, "init", "-q")
        self.git(manifests, "config", "user.email", "t@example.com")
        self.git(manifests, "config", "user.name", "t")
        self.git(manifests, "checkout", "-q", "-b", "default")
        self.git(manifests, "add", "-A")
        self.git(manifests, "commit", "-q", "--allow-empty", "-m", "manifest")
        if merge is not None:
            self.git(manifests, "config", "branch.default.merge", merge)
        return self.git(manifests, "rev-parse", "HEAD").stdout.strip()

    def synced_project(self, path):
        """Create one synced project: a real checkout with one commit."""
        target = self.work / path
        target.mkdir(parents=True, exist_ok=True)
        self.git(target, "init", "-q")
        self.git(target, "config", "user.email", "t@example.com")
        self.git(target, "config", "user.name", "t")
        (target / "README").write_text("%s\n" % path, encoding="utf-8")
        self.git(target, "add", "-A")
        self.git(target, "commit", "-q", "-m", "content")
        return self.git(target, "rev-parse", "HEAD").stdout.strip()

    def git(self, cwd, *args):
        return subprocess.run([GIT, "-C", str(cwd), *args], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=True)

    # -- driver -------------------------------------------------------------
    def shell_env(self):
        env = os.environ.copy()
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["LPTOOLS_TEST_ROOT"] = ROOT.as_posix()
        env["WORK"] = str(self.work)
        env["BUNDLE"] = str(self.bundle)
        env["REQUESTED"] = "android-15.0.0_r1"
        return env

    def run_script(self, script, env=None):
        return subprocess.run([BASH, "--noprofile", "--norc", "-c", script],
                              env=env or self.shell_env(), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=120)

    def export(self, requested="android-15.0.0_r1"):
        env = self.shell_env()
        env["REQUESTED"] = requested
        return self.run_script(DRIVER, env)

    @staticmethod
    def fields(result):
        out = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.isupper():
                out.setdefault(key, value)
        return out

    def pinned(self):
        path = self.bundle / "aosp-manifest.xml"
        self.assertTrue(path.is_file(), "aosp-manifest.xml was not written")
        return path

    def pinned_projects(self):
        root = ET.parse(self.pinned()).getroot()
        return {element.get("path") or element.get("name"): element.get("revision")
                for element in root.iter("project")}

    # -- tests --------------------------------------------------------------
    def test_default_revision_is_parsed_as_xml(self):
        path = self.bundle / "default.xml"
        path.write_text("<manifest><default\n revision='refs/heads/release/15'\n"
                        " remote='aosp'/></manifest>\n", encoding="utf-8")
        result = self.run_script('source "$LPTOOLS_TEST_ROOT/scripts/aosp-meta.sh"; '
                                 'lptools_aosp_default_revision "$BUNDLE/default.xml"')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "refs/heads/release/15")

    def test_pins_only_the_projects_that_were_synced(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
  <project name="platform/frameworks/base" path="frameworks/base"/>
  <project name="platform/system/core" path="system/core"/>
  <project name="platform/tools/lptools" path="tools/lptools"/>
</manifest>
""")
        manifest_rev = self.manifest_checkout("refs/tags/android-15.0.0_r1")
        want = {
            "build": self.synced_project("build"),
            "system/core": self.synced_project("system/core"),
        }
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["RC"], "0")
        self.assertEqual(fields["PROJECTS"], "2")
        self.assertEqual(fields["REV"], manifest_rev)
        self.assertEqual(fields["TAG"], "android-15.0.0_r1")
        self.assertEqual(fields["BRANCH"], "")
        self.assertEqual(self.pinned_projects(), want)
        self.assertIn("2 of 4", self.pinned().read_text(encoding="utf-8"))
        root = ET.parse(self.pinned()).getroot()
        # The manifest metadata the tree really carries is preserved.
        self.assertEqual(root.find("default").get("revision"),
                         "refs/tags/android-15.0.0_r1")
        self.assertEqual(root.find("remote").get("name"), "aosp")
        for revision in self.pinned_projects().values():
            self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_branch_comes_from_manifest_default_revision_not_repo_default(self):
        # Real trees put the default revision in an included file, while repo
        # still names the local manifest branch "default" -- never report that.
        (self.work / ".repo" / "manifests" / "defaults.xml").write_text(
            '<manifest>\n  <remote name="aosp" fetch=".."/>\n'
            '  <default revision="refs/heads/android15-release" remote="aosp"/>\n'
            '</manifest>\n', encoding="utf-8")
        self.write_manifest("""<manifest>
  <include name="defaults.xml"/>
  <project name="platform/build" path="build"/>
</manifest>
""")
        self.manifest_checkout("refs/heads/android15-release")
        self.synced_project("build")
        result = self.export(requested="android15-release")
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["BRANCH"], "android15-release")
        self.assertEqual(fields["TAG"], "")
        self.assertNotEqual(fields["BRANCH"], "default")

    def test_tag_survives_a_fetch_that_created_no_local_tag_ref(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
</manifest>
""")
        # A -c/--no-tags sync leaves branch.default.merge as the only record.
        self.manifest_checkout("refs/tags/android-15.0.0_r1")
        self.synced_project("build")
        self.assertEqual(
            self.git(self.work / ".repo" / "manifests", "tag").stdout.strip(), "")
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["TAG"], "android-15.0.0_r1")
        self.assertEqual(fields["BRANCH"], "")

    def test_local_manifest_wrapper_default_revision_is_used(self):
        self.write_manifest("""<manifest>
  <remote name="aosp" fetch=".."/>
  <project name="platform/build" path="build"/>
  <project name="platform/system/core" path="system/core"/>
</manifest>
""")
        # A wrapper manifest is the supported way to override the revision.
        self.write_local_manifest("""<manifest>
  <default revision="refs/heads/android15-release" remote="aosp"/>
</manifest>
""")
        self.manifest_checkout("refs/heads/android15-release")
        self.synced_project("build")
        self.synced_project("system/core")
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["BRANCH"], "android15-release")
        self.assertEqual(fields["PROJECTS"], "2")

    def test_no_synced_project_fails_instead_of_writing_an_empty_manifest(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
  <project name="platform/system/core" path="system/core"/>
</manifest>
""")
        self.manifest_checkout("refs/tags/android-15.0.0_r1")
        result = self.export()
        self.assertEqual(result.returncode, 1, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["RC"], "1")
        self.assertEqual(fields["PROJECTS"], "0")
        self.assertIn("WARN:aosp-meta:", result.stdout)
        self.assertIn("no synced project", result.stdout)
        self.assertFalse((self.bundle / "aosp-manifest.xml").exists(),
                         "an empty pinned manifest was shipped")
        self.assertEqual(fields["REV"], "")

    def test_checkout_without_a_commit_fails_the_export(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
</manifest>
""")
        self.manifest_checkout("refs/tags/android-15.0.0_r1")
        (self.work / "build").mkdir()
        self.git(self.work / "build", "init", "-q")  # unborn HEAD
        result = self.export()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("cannot pin a commit for synced project(s): build", result.stdout)
        self.assertFalse((self.bundle / "aosp-manifest.xml").exists())

    def test_repo_manifest_failure_is_fatal(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
</manifest>
""")
        self.manifest_checkout("refs/tags/android-15.0.0_r1")
        self.synced_project("build")
        (self.work / ".repo" / "manifest.xml").write_text(
            "<manifest><!-- broken -->\n", encoding="utf-8")
        result = self.export()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("repo manifest failed", result.stdout)
        self.assertFalse((self.bundle / "aosp-manifest.xml").exists())

    def test_missing_repo_metadata_fails_instead_of_reporting_zero(self):
        shutil.rmtree(self.work / ".repo")
        self.synced_project("build")
        result = self.export()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no repo metadata", result.stdout)
        self.assertEqual(self.fields(result)["PROJECTS"], "0")

    def test_export_aborts_a_set_e_build_on_a_provenance_failure(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
</manifest>
""")
        self.manifest_checkout("refs/tags/android-15.0.0_r1")
        script = """
set -euo pipefail
warn() { printf 'WARN:%s\\n' "$*" >&2; }
source "$LPTOOLS_TEST_ROOT/scripts/aosp-meta.sh"
lptools_export_meta "$WORK" "$BUNDLE"
printf 'REACHED-AFTER-EXPORT\\n'
"""
        result = self.run_script(script)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("REACHED-AFTER-EXPORT", result.stdout)

    def test_pinned_manifest_is_built_per_project_not_with_manifest_r(self):
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
  <project name="platform/system/core" path="system/core"/>
</manifest>
""")
        self.manifest_checkout("refs/tags/android-15.0.0_r1")
        self.synced_project("build")
        # The stand-in repo aborts on -r exactly like the real tool and blames
        # the unsynced project when it does, so success proves the pinned
        # manifest is assembled per checked-out project instead.
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fields(result)["PROJECTS"], "1")
        self.assertNotIn("unsynced project", result.stdout)


    def test_unverifiable_requested_ref_records_no_branch_or_tag(self):
        # No <default revision> and no branch.default.merge in the checkout: the
        # requested name matches no ref, so nothing may be guessed for it.
        self.write_manifest("""<manifest>
  <remote name="aosp" fetch=".."/>
  <project name="platform/build" path="build"/>
</manifest>
""")
        self.manifest_checkout()
        self.synced_project("build")
        result = self.export(requested="android15-release")
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["BRANCH"], "")
        self.assertEqual(fields["TAG"], "")
        self.assertIn("matches no ref", result.stdout)
        self.assertEqual(fields["PROJECTS"], "1")

    def test_exported_fields_feed_the_bundle_manifest(self):
        # scripts/make-manifest.py rejects an empty or half-pinned record, so a
        # successful package here proves the exported values are self-consistent.
        self.write_manifest(MANIFEST_HEAD + """  <project name="platform/build" path="build"/>
  <project name="platform/system/core" path="system/core"/>
</manifest>
""")
        manifest_rev = self.manifest_checkout("refs/tags/android-15.0.0_r1")
        sha = self.synced_project("build")
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        (self.bundle / "bin").mkdir()
        (self.bundle / "bin" / "lpmake").write_text("tool\n", encoding="utf-8")
        packager = ROOT / "scripts" / "make-manifest.py"
        packed = subprocess.run(
            [sys.executable, str(packager), "--dir", str(self.bundle),
             "--aosp-ref", "android-15.0.0_r1",
             "--aosp-branch", fields["BRANCH"],
             "--aosp-tag", fields["TAG"],
             "--aosp-manifest-rev", fields["REV"],
             "--aosp-pinned-projects", fields["PROJECTS"]],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(packed.returncode, 0, packed.stdout)
        manifest = json.loads((self.bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["aosp_ref"], "android-15.0.0_r1")
        self.assertEqual(manifest["aosp_tag"], "android-15.0.0_r1")
        self.assertEqual(manifest["aosp_manifest_revision"], manifest_rev)
        self.assertEqual(manifest["aosp_pinned_manifest"]["projects"], 1)
        self.assertEqual(manifest["aosp_pinned_manifest"]["file"], "aosp-manifest.xml")
        self.assertEqual(self.pinned_projects(), {"build": sha})

    def test_bare_default_revision_uses_repo_fetched_ref_kind(self):
        # A manifest may spell the revision without a refs/ prefix. repo records
        # what it actually fetched (refs/heads/main) in branch.default.merge;
        # that same name is evidence for calling it a branch, not a guess.
        self.write_manifest("""<manifest>
  <remote name="aosp" fetch=".."/>
  <default revision="main" remote="aosp"/>
  <project name="platform/build" path="build"/>
</manifest>
""")
        self.manifest_checkout("refs/heads/main")
        self.synced_project("build")
        result = self.export(requested="main")
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.fields(result)
        self.assertEqual(fields["BRANCH"], "main")
        self.assertEqual(fields["TAG"], "")
        self.assertNotIn("matches no ref", result.stdout)

    def rolling_checkout(self):
        self.write_manifest("""<manifest>
  <remote name="aosp" fetch=".."/>
  <default revision="android17-release" remote="aosp"/>
  <project name="platform/system/core" path="system/core"/>
</manifest>
""")
        self.manifest_checkout("refs/heads/android-latest-release")
        head = self.synced_project("system/core")
        return self.work / "system/core", head

    def test_rolling_branch_is_verified_against_synced_project_ref(self):
        project, head = self.rolling_checkout()
        self.git(project, "update-ref", "refs/remotes/aosp/android17-release", head)
        result = self.export(requested="android-latest-release")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fields(result)["BRANCH"], "android17-release")
        self.assertEqual(self.fields(result)["TAG"], "")

    def test_rolling_revision_with_no_matching_project_ref_stays_unknown(self):
        self.rolling_checkout()
        result = self.export(requested="android-latest-release")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fields(result)["BRANCH"], "")
        self.assertEqual(self.fields(result)["TAG"], "")

    def test_project_tag_classifies_bare_default_revision(self):
        project, head = self.rolling_checkout()
        self.git(project, "update-ref", "refs/tags/android17-release", head)
        result = self.export(requested="android-latest-release")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fields(result)["BRANCH"], "")
        self.assertEqual(self.fields(result)["TAG"], "android17-release")

    def test_ambiguous_project_branch_and_tag_are_not_guessed(self):
        project, head = self.rolling_checkout()
        self.git(project, "update-ref", "refs/remotes/aosp/android17-release", head)
        self.git(project, "update-ref", "refs/tags/android17-release", head)
        result = self.export(requested="android-latest-release")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fields(result)["BRANCH"], "")
        self.assertEqual(self.fields(result)["TAG"], "")

if __name__ == "__main__":
    unittest.main(verbosity=2)
