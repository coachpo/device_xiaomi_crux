#!/system/bin/sh
#
# Copyright (C) 2021 The PixelExperience Project
#
# SPDX-License-Identifier: Apache-2.0
#

# Optional compatibility glue only: the official crux fastboot ROM still exposes
# discrete system/vendor partitions, so this helper must not be read as proof of
# a real crux super-partition layout.
SUPER="/dev/block/by-name/system"
MOUNT_POINT="/tmp/super-mnt"
SUPER_DUMMY_IMAGE="/tmp/super_dummy.img"

mkdir -p "$MOUNT_POINT"

if [ -f "$SUPER_DUMMY_IMAGE" ] && mount "$SUPER" "$MOUNT_POINT" 2>/dev/null; then
    echo "Detected stock /system behind compatibility super path, flashing super_dummy.img"
    umount "$MOUNT_POINT"
    dd if="$SUPER_DUMMY_IMAGE" of="$SUPER"
fi

rmdir "$MOUNT_POINT"
