#!/usr/bin/env bash
# Print the provenance of the bundles in <dist> to the GitHub step summary
# (or stdout when run locally), so every CI run shows which AOSP commit and
# branch it actually built from without opening a single artifact.
#
#   scripts/ci-summary.sh dist linux-x86_64
#   scripts/ci-summary.sh dist android
set -euo pipefail

DIST=${1:-dist}
LABEL=${2:-}
OUT=${GITHUB_STEP_SUMMARY:--}

python3 - "$DIST" "$LABEL" "$OUT" <<'PY'
import glob, json, os, sys

dist, label, out = sys.argv[1], sys.argv[2], sys.argv[3]
rows = []
for path in sorted(glob.glob(os.path.join(dist, "lptools-*", "manifest.json"))):
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        continue
    if label and label not in m.get("platform", ""):
        continue
    rows.append(m)

lines = ["### lptools bundle provenance", ""]
if not rows:
    lines += ["No matching bundle manifest found in " + dist + "."]
else:
    first = rows[0]
    lines += [
        "| field | value |",
        "| --- | --- |",
        "| requested ref | " + str(first.get("aosp_ref", "")) + " |",
        "| branch fetched | " + str(first.get("aosp_branch") or "unknown") + " |",
    ]
    if first.get("aosp_tag"):
        lines.append("| tag | " + first["aosp_tag"] + " |")
    lines += [
        "| manifest commit | " + str(first.get("aosp_manifest_revision") or "unknown") + " |",
        "| builder commit | " + str((first.get("builder") or {}).get("commit", "")) + " |",
        "",
        "| platform | kind | tools |",
        "| --- | --- | --- |",
    ]
    for m in rows:
        lines.append("| %s | %s | %s |" % (
            m.get("platform", ""), m.get("kind", ""),
            ", ".join(sorted(m.get("tools", {}))) or "-"))
lines.append("")

text = "\n".join(lines) + "\n"
if out in ("/dev/stdout", "-", ""):
    sys.stdout.write(text)
else:
    with open(out, "a", encoding="utf-8") as f:
        f.write(text)
PY
