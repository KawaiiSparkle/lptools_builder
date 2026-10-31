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
  lptools_lunch "${1:-lptools_host}"
}

# The caller creates this before run_beat so its EXIT trap can always remove it.
LP_HOST_PRODUCT_DIR=
lptools_prepare_host_product() {
  local work="$1" templates="$2" requested="${3:-lptools_host}"
  case "$requested" in
    lptools_host|lptools_host-*) ;;
    *) return 0 ;;
  esac
  mkdir -p "$work/device" || return $?
  LP_HOST_PRODUCT_DIR=$(mktemp -d "$work/device/lptools-host.XXXXXX") || return $?
  cp "$templates/AndroidProducts.mk" "$templates/lptools_host.mk" "$LP_HOST_PRODUCT_DIR/" || return $?
  log "host product: lptools_host (exclude device bionic/apex/ modules)"
}

lptools_cleanup_host_product() {
  if [ -n "$LP_HOST_PRODUCT_DIR" ]; then
    rm -rf -- "$LP_HOST_PRODUCT_DIR"
    LP_HOST_PRODUCT_DIR=
  fi
}
