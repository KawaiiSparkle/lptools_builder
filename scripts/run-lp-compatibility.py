#!/usr/bin/env python3
"""Run latest readers against every historical writer; fail on missing coverage."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aosp", required=True)
    parser.add_argument("--latest-bin", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    out = Path(args.out).resolve()
    historical, matrix = out / "historical", out / "matrix"
    out.mkdir(parents=True, exist_ok=True)
    latest_bin = Path(args.latest_bin).resolve()
    latest_manifest = json.loads((latest_bin.parent / "manifest.json").read_text(encoding="utf-8"))
    build_rc = subprocess.call([sys.executable, "-u", str(root / "scripts/build-historical-writers.py"),
                                "--aosp", args.aosp, "--latest-bin", str(latest_bin),
                                "--out", str(historical)])
    listing = json.loads((historical / "writer-list.json").read_text(encoding="utf-8"))
    writers = listing["writers"]
    built = [w for w in writers if w["status"] == "built"]
    failed = [w for w in writers if w["status"] != "built"]
    matrix_rc = None
    if built:
        command = [sys.executable, "-u", str(root / "tests/lp-compatibility.py"),
                   "--latest-bin", str(latest_bin), "--out", str(matrix)]
        for writer in built:
            command.extend(["--writer", writer["writer_arg"]])
        matrix_rc = subprocess.call(command)
    matrix_path = matrix / "lp-compatibility.json"
    result = json.loads(matrix_path.read_text(encoding="utf-8")) if matrix_path.is_file() else None
    complete = len(writers) == 7 and len(built) == 7 and not failed and build_rc == 0
    passed = (complete and matrix_rc == 0 and bool(result) and result["verdict"]["compatible"]
              and result["summary"]["failed_optional"] == 0)
    report = {"schema": "lp-cross-version/1", "status": "pass" if passed else "fail",
              "coverage_complete": complete, "latest": latest_manifest,
              "historical_build_exit": build_rc, "matrix_exit": matrix_rc,
              "writers": writers, "matrix": result,
              "scope": "Unmodified historical liblp and lpmake sources, linked to recorded latest support libraries; synthetic raw/sparse super fixtures, not device flashing or OTA snapshot semantics."}
    (out / "compatibility-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    text = ["# Latest-release LP cross-version test", "",
            "**%s** — %d/7 historical writers built." % (report["status"].upper(), len(built)), "",
            "Latest manifest: %s (%s)." % (latest_manifest["aosp_ref"], latest_manifest["aosp_manifest_revision"]),
            "", "| AOSP ref | status | source commits (core / extras) | error |",
            "| --- | --- | --- | --- |"]
    for writer in writers:
        commits = writer.get("commits", {})
        text.append("| %s | %s | %s / %s | %s |" % (writer["ref"], writer["status"],
                    commits.get("platform/system/core", "-"), commits.get("platform/system/extras", "-"),
                    (writer.get("error") or "").replace("|", "/").replace("\n", " ")))
    text.extend(["", report["scope"], ""])
    if result:
        text.append((matrix / "lp-compatibility.md").read_text(encoding="utf-8"))
    summary = "\n".join(text)
    (out / "compatibility-report.md").write_text(summary, encoding="utf-8")
    print(summary, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
