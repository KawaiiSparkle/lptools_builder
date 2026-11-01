#!/usr/bin/env python3
"""Regenerate the checkout-free CI workflow from exact local script bytes."""
import base64
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ".github/actions/prepare/action.yml",
    "scripts/free-disk.sh", "scripts/free-disk-darwin.sh", "scripts/prepare-swap.sh",
    "scripts/log.sh", "scripts/aosp-meta.sh", "scripts/aosp-lunch.sh",
    "scripts/build-linux.sh", "scripts/smoke-test.sh", "scripts/make-manifest.py",
    "scripts/build-historical-writers.py", "scripts/run-lp-compatibility.py",
    "tests/lp-compatibility.py", "tools/check_projects.py",
    "config/projects-latest.txt", "config/host-product/AndroidProducts.mk",
    "config/host-product/lptools_host.mk",
]


class Dumper(yaml.SafeDumper):
    pass


def string_repr(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data,
                                   style="|" if "\n" in data else None)


Dumper.add_representer(str, string_repr)


def workflow():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in sorted(FILES):
            data = (ROOT / name).read_bytes()
            item = tarfile.TarInfo(name)
            item.size, item.mtime = len(data), 0
            item.mode = 0o755 if name.endswith((".py", ".sh")) else 0o644
            archive.addfile(item, io.BytesIO(data))
    raw = gzip.compress(stream.getvalue(), mtime=0)
    payload = base64.b64encode(raw).decode()
    steps = []
    for i, offset in enumerate(range(0, len(payload), 14000)):
        chunk = payload[offset:offset + 14000]
        mode = ">" if i == 0 else ">>"
        command = 'set -euo pipefail\ncat %s "$RUNNER_TEMP/lptools-payload.b64" <<\'PAYLOAD\'\n' % mode
        command += "\n".join(chunk[j:j + 120] for j in range(0, len(chunk), 120)) + "\nPAYLOAD\n"
        steps.append({"name": "Restore source payload %d" % (i + 1), "shell": "bash", "run": command})
    unpack = '''set -euo pipefail
python3 - <<'PY'
import base64, hashlib, io, os, tarfile
from pathlib import Path
raw = base64.b64decode((Path(os.environ["RUNNER_TEMP"]) / "lptools-payload.b64").read_bytes())
assert hashlib.sha256(raw).hexdigest() == "DIGEST"
root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
    for item in archive.getmembers():
        dest = (root / item.name).resolve()
        assert item.isfile() and dest.is_relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive.extractfile(item).read())
        dest.chmod(item.mode)
PY
'''.replace("DIGEST", hashlib.sha256(raw).hexdigest())
    steps += [
        {"name": "Verify and unpack build scripts", "shell": "bash", "run": unpack},
        {"uses": "./.github/actions/prepare"},
        {"name": "Build current latest-release tools", "shell": "bash", "run": '''set -euo pipefail
bash scripts/build-linux.sh --ref "$AOSP_REF" --jobs 4 \\
  --project-list "$GITHUB_WORKSPACE/config/projects-latest.txt" \\
  --work "$GITHUB_WORKSPACE/aosp" --out "$GITHUB_WORKSPACE/dist"
'''},
        {"name": "Build historical writers and run partition round trips", "shell": "bash", "run": '''set -euo pipefail
source scripts/log.sh
run_beat "historical builds and compatibility matrix" python3 -u scripts/run-lp-compatibility.py \\
  --aosp "$GITHUB_WORKSPACE/aosp" \\
  --latest-bin "$GITHUB_WORKSPACE/dist/lptools-linux-x86_64/bin" \\
  --out "$GITHUB_WORKSPACE/compat-out"
'''},
        {"name": "Preserve reports and tool provenance", "if": "always()", "uses": "actions/upload-artifact@v4",
         "with": {"name": "lp-latest-release-compatibility", "compression-level": 0, "if-no-files-found": "warn",
                  "path": "\n".join([
                      "compat-out/compatibility-report.*", "compat-out/matrix/lp-compatibility.*",
                      "compat-out/matrix/logs/", "compat-out/historical/writer-list.json",
                      "compat-out/historical/*/manifest.json", "compat-out/historical/logs/",
                      "compat-out/historical/source/*/pin.json", "dist/*.tar.gz", "dist/*.sha256",
                      "dist/lptools-linux-x86_64/manifest.json", "dist/lptools-linux-x86_64/aosp-manifest.xml"])}}
    ]
    return {"name": "LP latest-release compatibility",
            "on": {"push": {"branches": ["compat/latest-release"]},
                   "workflow_dispatch": {"inputs": {"latest_ref": {
                       "description": "Official AOSP manifest ref to test", "default": "android-latest-release",
                       "required": True}}}},
            "permissions": {"contents": "read"},
            "concurrency": {"group": "lp-compat-${{ github.ref }}", "cancel-in-progress": True},
            "jobs": {"compatibility": {"runs-on": "ubuntu-24.04", "timeout-minutes": 180,
                     "env": {"AOSP_REF": "${{ inputs.latest_ref || 'android-latest-release' }}",
                             "LPTOOLS_PRODUCT": "lptools_host", "LPTOOLS_BUILDER_SHA": "${{ github.sha }}",
                             "LPTOOLS_BUILDER_BRANCH": "${{ github.ref_name }}",
                             "LPTOOLS_BUILDER_REF": "git@github.com:${{ github.repository }}.git",
                             "PYTHONUNBUFFERED": "1"}, "steps": steps}}}


def main():
    target = ROOT / ".github/workflows/lp-compatibility.yml"
    header = ("# Generated by scripts/make-compat-workflow.py; regenerate after embedded file changes.\n"
              "# Exact source payload: no GitHub repository fetch or runner SSH key is required.\n")
    target.write_text(header + yaml.dump(workflow(), Dumper=Dumper, sort_keys=False, width=120),
                      encoding="utf-8", newline="\n")
    print("wrote %s" % target)


if __name__ == "__main__":
    main()
