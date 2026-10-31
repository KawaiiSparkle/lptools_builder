#!/usr/bin/env bash
# Reclaim disk space on a hosted macOS runner before an AOSP checkout.
#
# Capacity and reclaimable space depend on the runner image and AOSP ref.
# Use only on disposable hosted runners: cleanup removes developer caches and
# preinstalled tools, and is not intended for a workstation.
set -euo pipefail

log() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }

before=$(df -Pk / | awk 'NR==2 {print $4}')
log "free before: $((before / 1024 / 1024)) GB"

log "removing Xcode derived data, device support and simulator caches"
rm -rf "$HOME/Library/Developer/Xcode/DerivedData" 2>/dev/null || true
rm -rf "$HOME/Library/Developer/Xcode/iOS DeviceSupport" 2>/dev/null || true
rm -rf "$HOME/Library/Developer/CoreSimulator/Caches" 2>/dev/null || true
rm -rf /Library/Developer/CoreSimulator/Caches/* 2>/dev/null || sudo rm -rf /Library/Developer/CoreSimulator/Caches/* 2>/dev/null || true

log "removing package-manager and tool caches"
rm -rf "$HOME/Library/Caches/Homebrew" 2>/dev/null || true
brew cleanup -s 2>/dev/null || true
rm -rf "$HOME/.gradle/caches" 2>/dev/null || true
rm -rf "$HOME/.nuget/packages" 2>/dev/null || true

log "removing pre-installed toolchains that an AOSP build does not use"
for p in /usr/local/share/dotnet /usr/local/lib/node_modules /opt/homebrew/lib/node_modules; do
  [ -e "$p" ] && sudo rm -rf "$p" 2>/dev/null || true
done

after=$(df -Pk / | awk 'NR==2 {print $4}')
log "free after:  $((after / 1024 / 1024)) GB"
