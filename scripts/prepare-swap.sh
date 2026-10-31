#!/usr/bin/env bash
# Ensure a minimum total Linux swap capacity without changing existing swap.
# Swap is a memory fallback: the new file consumes disk space.
set -euo pipefail

fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

swap_gb=${SWAP_GB:-12}
[[ "$swap_gb" =~ ^(0|[1-9][0-9]{0,3})$ ]] \
  || fail 'SWAP_GB must be a whole number from 0 to 9999 (GiB).'
if (( swap_gb == 0 )); then
  echo 'Swap preparation disabled; existing swap is unchanged.'
  exit 0
fi
[[ $(uname -s) == Linux ]] || fail 'Swap preparation is supported only on Linux.'

# Query sizes in bytes, including every active file, partition or zram device.
active_sizes=$(swapon --show=SIZE --bytes --noheadings --raw)
active_bytes=0
while IFS= read -r size; do
  [[ -z "$size" ]] && continue
  [[ "$size" =~ ^[0-9]+$ && ${#size} -le 15 ]] \
    || fail "Invalid active swap size: $size"
  active_bytes=$((active_bytes + 10#$size))
done <<< "$active_sizes"
target_bytes=$((swap_gb * 1024 * 1024 * 1024))
printf 'Swap target: %s GiB; currently active: %s MiB.\n' \
  "$swap_gb" "$((active_bytes / 1024 / 1024))"
if (( active_bytes >= target_bytes )); then
  echo 'Existing swap meets the target; no file will be allocated.'
  exit 0
fi

# mkswap reserves a header page; round up to MiB so usable capacity meets target.
page_bytes=$(getconf PAGESIZE)
[[ "$page_bytes" =~ ^[0-9]+$ && ${#page_bytes} -le 7 ]] \
  || fail 'Could not determine the system page size.'
additional_mib=$(((target_bytes - active_bytes + page_bytes + 1048575) / 1048576))
# Leave at least 1 GiB free on the selected filesystem. This is not a build-space
# guarantee; the actual checkout/build requirement must be budgeted separately.
reserve_kib=$((1024 * 1024))
required_kib=$((additional_mib * 1024 + reserve_kib))

if [[ -n ${SWAP_DIRECTORY:-} ]]; then
  candidates=("$SWAP_DIRECTORY")
else
  candidates=(/mnt "${RUNNER_TEMP:-}" "${TMPDIR:-}" /tmp)
fi
swap_base=
for candidate in "${candidates[@]}"; do
  [[ -n "$candidate" && -d "$candidate" ]] || continue
  if ! disk_info=$(df -Pk -- "$candidate"); then
    printf 'Cannot inspect free space at %s; trying the next location.\n' "$candidate" >&2
    continue
  fi
  available_kib=$(awk 'NR == 2 { print $4 }' <<< "$disk_info")
  [[ "$available_kib" =~ ^[0-9]+$ && ${#available_kib} -le 15 ]] \
    || fail "Invalid available disk space at $candidate"
  printf 'Disk at %s: %s MiB free; need %s MiB plus 1024 MiB reserve.\n' \
    "$candidate" "$((available_kib / 1024))" "$additional_mib"
  if (( available_kib >= required_kib )); then
    swap_base=$candidate
    break
  fi
done
[[ -n "$swap_base" ]] || fail 'Not enough disk space to add swap while retaining 1 GiB free.'
# Match the kernel's absolute path even if the selected directory is a symlink.
swap_base=$(cd -- "$swap_base" && pwd -P)

swap_dir=
swap_file=
activated=0
activation_attempted=0
cleanup() {
  local result=$? names name decoded_name safe_to_remove=1
  trap - EXIT
  if [[ -n "$swap_dir" && $activated == 0 ]]; then
    # A signal can arrive just after swapon succeeds but before activated is set.
    # Never unlink active swap, or a file whose activation state is unknown.
    if (( activation_attempted )); then
      if names=$(swapon --show=NAME --noheadings --raw); then
        while IFS= read -r name; do
          # util-linux raw output escapes unsafe path bytes as \xNN.
          printf -v decoded_name '%b' "$name"
          if [[ "$decoded_name" == "$swap_file" ]]; then safe_to_remove=0; fi
        done <<< "$names"
      else
        safe_to_remove=0
      fi
    fi
    if (( safe_to_remove )); then
      if sudo rm -f -- "$swap_file" && sudo rmdir -- "$swap_dir"; then
        echo 'Removed the incomplete swap allocation.'
      else
        printf '::warning::Could not remove incomplete allocation: %s\n' "$swap_dir" >&2
      fi
    else
      printf '::warning::Retaining possibly active swap at %s for manual inspection.\n' "$swap_file" >&2
    fi
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Root-owned private directory plus mktemp prevents clobbering /mnt/swapfile,
# following a pre-existing symlink, or exposing a partially allocated file.
swap_dir=$(sudo mktemp -d "$swap_base/lptools-swap.XXXXXX")
swap_file=$swap_dir/swapfile
printf 'Allocating %s MiB of additional swap at %s (uses disk space).\n' \
  "$additional_mib" "$swap_file"
sudo install -m 600 /dev/null "$swap_file"
if ! sudo fallocate -l "${additional_mib}M" "$swap_file"; then
  echo 'fallocate failed; retrying allocation with dd.'
  sudo dd if=/dev/zero of="$swap_file" bs=1M count="$additional_mib" status=progress
fi
sudo chmod 600 "$swap_file"
sudo mkswap "$swap_file"
activation_attempted=1
sudo swapon "$swap_file"
activated=1
echo 'Additional swap activated; all existing swap is unchanged.'
swapon --show
