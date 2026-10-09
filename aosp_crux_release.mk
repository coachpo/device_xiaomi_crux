# Public stock-ABL product. Storage routing is selected by TARGET_PRODUCT so
# debug variants cannot accidentally inherit the U-Boot development partitions.
$(call inherit-product, device/xiaomi/crux/aosp_crux.mk)

# Select the standard vendor Recovery updater for this public non-A/B product.
$(call inherit-product, $(SRC_TARGET_DIR)/product/non_ab_device.mk)
PRODUCT_SYSTEM_DEFAULT_PROPERTIES += persist.sys.recovery_update=true

PRODUCT_NAME := aosp_crux_release
PRODUCT_DEVICE := crux
