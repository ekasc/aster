#!/bin/sh
# Inspect a Waydroid Android userspace from the Alpine-on-GKI guest.
# Read-only: mounts /system and /vendor, reports facts, then powers off.
mknod /dev/console c 5 1 2>/dev/null
mknod /dev/null c 1 3 2>/dev/null
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
export PATH=/sbin:/bin:/usr/sbin:/usr/bin
for module in virtio_pci_modern_dev virtio_pci_legacy_dev virtio_pci virtio_blk; do
    output=$(insmod "/lib/modules/$module.ko" 2>&1)
    echo "ASTER-WAYDROID|insmod=$module rc=$? detail=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-120)"
done
for block in /sys/class/block/*; do
    name=${block##*/}
    [ -r "$block/dev" ] || continue
    case "$name" in loop*|ram*) continue ;; esac
    major=$(cut -d: -f1 "$block/dev")
    minor=$(cut -d: -f2 "$block/dev")
    mknod "/dev/$name" b "$major" "$minor" 2>/dev/null
done
echo "ASTER-WAYDROID|modules_dir=$(ls /lib/modules 2>/dev/null | head -3 | tr '\n' ' ')"
echo "ASTER-WAYDROID|dmesg=$(dmesg 2>/dev/null | tail -6 | tr '\n' '~' | cut -c1-300)"
echo "ASTER-WAYDROID|blocks=$(ls /sys/class/block | tr '\n' ' ')"
for device in /dev/vda /dev/vdb; do
    echo "ASTER-WAYDROID|device=$device present=$([ -b "$device" ] && echo yes || echo no)"
done
mkdir -p /system /vendor /data
mount -t ext4 -o ro /dev/vda /system 2>/dev/null \
    && echo "ASTER-WAYDROID|system_mount=ok" || echo "ASTER-WAYDROID|system_mount=failed"
mount -t ext4 -o ro /dev/vdb /vendor 2>/dev/null \
    && echo "ASTER-WAYDROID|vendor_mount=ok" || echo "ASTER-WAYDROID|vendor_mount=failed"
echo "ASTER-WAYDROID|system_top=$(ls /system 2>/dev/null | head -12 | tr '\n' ' ')"
echo "ASTER-WAYDROID|system_system=$(ls /system/system 2>/dev/null | head -6 | tr '\n' ' ')"
echo "ASTER-WAYDROID|system_bin=$(ls /system/bin 2>/dev/null | head -6 | tr '\n' ' ')"
echo "ASTER-WAYDROID|vendor_top=$(ls /vendor 2>/dev/null | head -12 | tr '\n' ' ')"
echo "ASTER-WAYDROID|vendor_bin=$(ls /vendor/bin 2>/dev/null | head -8 | tr '\n' ' ')"
for init in /system/bin/init /vendor/bin/init /system/system/bin/init; do
    [ -e "$init" ] && echo "ASTER-WAYDROID|init=$init present"
done
echo "ASTER-WAYDROID|vendor_rc=$(ls /vendor/etc/init 2>/dev/null | head -6 | tr '\n' ' ')"
echo "ASTER-WAYDROID|vendor_lib64=$(ls /vendor/lib64 2>/dev/null | head -8 | tr '\n' ' ')"
echo "ASTER-WAYDROID|vendor_hw=$(ls /vendor/lib64/hw 2>/dev/null | head -8 | tr '\n' ' ')"
echo "ASTER-WAYDROID|release=$(sed -n 's/^ro.build.version.release=//p' /system/build.prop 2>/dev/null | head -1)"
echo "ASTER-WAYDROID|sdk=$(sed -n 's/^ro.build.version.sdk=//p' /system/build.prop 2>/dev/null | head -1)"
echo "ASTER-WAYDROID|fingerprint=$(sed -n 's/^ro.build.fingerprint=//p' /system/build.prop 2>/dev/null | head -1)"
echo "ASTER-WAYDROID|system_rc_count=$(ls /system/etc/init 2>/dev/null | wc -l)"
echo "ASTER-WAYDROID|vendor_rc_count=$(ls /vendor/etc/init 2>/dev/null | wc -l)"
echo "ASTER-WAYDROID|waydroid_rc=$(ls /system/etc/init /vendor/etc/init 2>/dev/null | grep -ci waydroid)"
echo "ASTER-WAYDROID|halium=$(ls /vendor/lib64 2>/dev/null | grep -ci hybris)"
echo "ASTER-WAYDROID|gralloc=$(ls /vendor/lib64/hw 2>/dev/null | grep -ci gralloc)"
echo "ASTER-WAYDROID|stop"
sync
poweroff -f
