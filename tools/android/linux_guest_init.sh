#!/bin/sh
# PID 1 for the Alpine-on-GKI guest. Prints observable facts and stops.
# No ASTER-BOOT marker: this is Linux, not a booted Android framework.
exec > /dev/ttyAMA0 2>&1
echo "ASTER-LINUX-GUEST|start"
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
mount -t devtmpfs devtmpfs /dev 2>/dev/null
for module in virtio_mmio virtio_pci virtio_blk virtio_rng; do
    modprobe "$module" 2>/dev/null
done
echo "ASTER-LINUX-GUEST|uname=$(uname -r)"
echo "ASTER-LINUX-GUEST|binder=$([ -e /dev/binder ] && echo node || echo absent)"
echo "ASTER-LINUX-GUEST|binderfs=$([ -e /sys/fs/binder ] && echo present || echo absent)"
grep -q binder /proc/filesystems && echo "ASTER-LINUX-GUEST|filesystems=binder" || echo "ASTER-LINUX-GUEST|filesystems=none"
mkdir -p /dev/binderfs
mount -t binder binder /dev/binderfs 2>/dev/null && echo "ASTER-LINUX-GUEST|binderfs_mount=ok" || echo "ASTER-LINUX-GUEST|binderfs_mount=failed"
ls /dev/binderfs 2>/dev/null | tr '\n' ' ' | sed 's/^/ASTER-LINUX-GUEST|binderfs_nodes=/'
echo
mount -t tmpfs tmpfs /newroot/tmp 2>/dev/null
mount --move /proc /newroot/proc 2>/dev/null
mount --move /sys /newroot/sys 2>/dev/null
mount --move /dev /newroot/dev 2>/dev/null
exec switch_root /newroot /bin/sh -c '
    echo "ASTER-LINUX-GUEST|userspace=alpine release=$(cat /etc/alpine-release 2>/dev/null)"
    echo "ASTER-LINUX-GUEST|apk=$(command -v apk || echo absent)"
    echo "ASTER-LINUX-GUEST|id=$(id -u)"
    echo "ASTER-LINUX-GUEST|stop"
    poweroff -f
'
