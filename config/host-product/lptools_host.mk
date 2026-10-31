# Keep the stock module product and its board; build only host installation goals.
$(call inherit-product, $(SRC_TARGET_DIR)/product/module_arm64.mk)
PRODUCT_NAME := lptools_host

# Host libraries need bionic headers, but never the device runtime APEX itself.
# Blueprint uses longest matching path prefixes; unmatched files remain allowed.
PRODUCT_SOURCE_ROOT_DIRS += -bionic/apex/
