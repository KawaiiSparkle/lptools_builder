#!/usr/bin/env bash
# Reclaim disk on GitHub-hosted Ubuntu runners.
#
# Space requirements and reclaimable capacity vary by runner image and AOSP ref.
# This removes preinstalled toolchains on disposable GitHub-hosted machines.
# Do not use this cleanup on a workstation or shared self-hosted runner.
#
#   scripts/free-disk.sh [--aggressive]
set -euo pipefail

AGGRESSIVE=0
[ "${1:-}" = "--aggressive" ] && AGGRESSIVE=1

before=$(df -Pk / | awk "NR==2 {print \$4}")
echo "free before: $((before / 1024 / 1024)) GB"

# Big, and irrelevant to a C/C++ AOSP host build.
sudo rm -rf \
  /usr/share/dotnet \
  /usr/local/lib/android \
  /usr/local/share/boost \
  /opt/ghc \
  /opt/hostedtoolcache/CodeQL \
  /usr/local/share/chromium \
  /opt/microsoft \
  /opt/google \
  2>/dev/null || true

# Docker images and apt caches.
sudo docker image prune -a -f >/dev/null 2>&1 || true
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/* 2>/dev/null || true

if [ "$AGGRESSIVE" -eq 1 ]; then
  # Last resort on small runners: drop the other hosted toolchains too.
  sudo rm -rf /opt/hostedtoolcache/Java_Temurin-Hotspot_jdk \
              /opt/hostedtoolcache/PyPy \
              /opt/hostedtoolcache/Ruby 2>/dev/null || true
fi

# Swap is managed separately by scripts/prepare-swap.sh.

after=$(df -Pk / | awk "NR==2 {print \$4}")
echo "free after : $((after / 1024 / 1024)) GB"
echo "memory     : $(free -h | awk "/Mem:/ {print \$2}") RAM, $(free -h | awk "/Swap:/ {print \$2}") swap"
