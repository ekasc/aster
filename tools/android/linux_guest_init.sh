#!/bin/sh
# PID 1 for the Alpine-on-GKI guest. Prints observable facts, then stops.
# The GKI kernel has no devtmpfs, so console nodes are created explicitly here;
# Android's own first-stage init does the same. No ASTER-BOOT marker: this is
# Linux, not a booted Android framework.
mknod /dev/console c 5 1 2>/dev/null
mknod /dev/null c 1 3 2>/dev/null
echo "ASTER-LINUX-GUEST|start"
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
for module in /modules/virtio_pci.ko /modules/virtio_blk.ko; do
    if [ -f "$module" ]; then
        insmod "$module" && echo "ASTER-LINUX-GUEST|insmod=$(basename "$module")=ok"
    fi
done
echo "ASTER-LINUX-GUEST|uname=$(uname -r)"
if grep -q binder /proc/filesystems 2>/dev/null; then
    echo "ASTER-LINUX-GUEST|binder=kernel_support"
else
    echo "ASTER-LINUX-GUEST|binder=absent"
fi
mkdir -p /dev/binderfs
if mount -t binder binder /dev/binderfs 2>/dev/null; then
    echo "ASTER-LINUX-GUEST|binderfs_mount=ok"
    echo "ASTER-LINUX-GUEST|binderfs_nodes=$(ls /dev/binderfs 2>/dev/null | tr '\n' ' ')"
else
    echo "ASTER-LINUX-GUEST|binderfs_mount=failed"
fi
echo "ASTER-LINUX-GUEST|release=$(cat /etc/alpine-release 2>/dev/null)"
echo "ASTER-LINUX-GUEST|apk=$(command -v apk || echo absent)"
echo "ASTER-LINUX-GUEST|uid=$(id -u)"
echo "ASTER-LINUX-GUEST|stop"
sync
poweroff -f
