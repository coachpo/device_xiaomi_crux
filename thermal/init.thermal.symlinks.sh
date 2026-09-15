#!/vendor/bin/sh

set -e

for f in /sys/class/thermal/thermal_zone*
do
  [ -d "$f" ] || continue
  tz_name=$(cat "$f/type")
  ln -sfn "$f" "/dev/thermal/tz-by-name/$tz_name"
done
for f in /sys/class/thermal/cooling_device*
do
  [ -d "$f" ] || continue
  cdev_name=$(cat "$f/type")
  ln -sfn "$f" "/dev/thermal/cdev-by-name/$cdev_name"
done
setprop vendor.thermal.link_ready 1
