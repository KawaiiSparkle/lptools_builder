#!/usr/bin/env bash
# Source after log.sh. Run lunch in the caller's shell: it exports build settings.
# Do not pipe lunch to tee or capture it with command substitution.
lptools_lunch() {
  local requested="$1" product variant candidate rc=1
  local candidates=()
  LP_LUNCH_COMBO=
  case "$requested" in
    *-*-*) candidates=("$requested") ;;
    *-eng|*-userdebug|*-user)
      product=${requested%-*}
      variant=${requested##*-}
      candidates=("$product-trunk_staging-$variant" "$requested")
      ;;
    *-*) warn "invalid lunch product/combo: $requested"; return 2 ;;
    *) candidates=("$requested-trunk_staging-eng" "$requested-eng") ;;
  esac
  for candidate in "${candidates[@]}"; do
    log "trying lunch $candidate"
    # Keep stdout/stderr live and retain TARGET_* / ANDROID_* exports for m.
    if lunch "$candidate"; then
      LP_LUNCH_COMBO=$candidate
      log "lunch $LP_LUNCH_COMBO"
      return 0
    else
      rc=$?
      warn "lunch $candidate failed (exit $rc)"
    fi
  done
  warn "lunch failed; see each attempt above (requested: $requested)"
  return "$rc"
}

# Configure a partial tree before lunch evaluates the product. Host tools do not
# need a GSI image or its VNDK snapshots. Soong still rejects missing dependencies
# when they are required by one of the requested build targets.
lptools_host_lunch() {
  export TARGET_BUILD_UNBUNDLED=true
  export ALLOW_MISSING_DEPENDENCIES=true
  # A host-only tree has no ART dex2oat dependency for device app preoptimization.
  export WITH_DEXPREOPT=false
  lptools_lunch "${1:-module_arm64}"
}
