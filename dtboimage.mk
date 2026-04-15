$(BOARD_PREBUILT_DTBOIMAGE): $(DEVICE_PATH)/prebuilt/dtbo.img
	@echo "Using stock prebuilt dtbo.img"
	mkdir -p $(dir $@)
	cp $(DEVICE_PATH)/prebuilt/dtbo.img $@
