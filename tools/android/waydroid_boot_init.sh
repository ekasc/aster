#!/bin/sh
# Attempt a real Android userspace from the Waydroid ARM64 images.
# Runs Android init as PID 1 of a new PID+mount namespace, which is how a
# container host starts it. Reports what actually happened; never claims boot.
export PATH=/sbin:/bin:/usr/sbin:/usr/bin
mknod /dev/console c 5 1 2>/dev/null
mknod /dev/null c 1 3 2>/dev/null
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
for module in virtio_pci_modern_dev virtio_pci_legacy_dev virtio_pci virtio_blk; do
    insmod "/lib/modules/$module.ko" 2>/dev/null
done
for block in /sys/class/block/*; do
    name=${block##*/}
    case "$name" in loop*|ram*) continue ;; esac
    [ -r "$block/dev" ] || continue
    mknod "/dev/$name" b "$(cut -d: -f1 "$block/dev")" "$(cut -d: -f2 "$block/dev")" 2>/dev/null
done
mkdir -p /android /data
mount -t ext4 -o ro /dev/vda /android 2>/dev/null || { echo "ASTER-WAYDROID|system_mount=failed"; poweroff -f; }
mount -t ext4 -o ro /dev/vdb /android/vendor 2>/dev/null && echo "ASTER-WAYDROID|vendor_mount=ok"
mount -t tmpfs tmpfs /android/dev 2>/dev/null
mount -t tmpfs tmpfs /android/proc 2>/dev/null
mount -t tmpfs tmpfs /android/sys 2>/dev/null
mount -t tmpfs -o size=2g tmpfs /android/data 2>/dev/null
mknod /android/dev/console c 5 1 2>/dev/null
mknod /android/dev/null c 1 3 2>/dev/null
echo "ASTER-WAYDROID|init_start=$(date +%s)"
echo "ASTER-WAYDROID|cmdline=$(cat /proc/cmdline)"
exec unshare -pf --mount-proc=/android/proc chroot /android /system/bin/init
