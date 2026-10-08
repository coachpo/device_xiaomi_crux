#!/vendor/bin/sh
# The SSC registry file proxy runs as system. Preserve factory calibration
# bytes and modes while making every registry group accessible to that owner.
registry=/mnt/vendor/persist/sensors/registry/registry
if [ -d "$registry" ]; then
    /vendor/bin/chown -R system:system "$registry"
fi
