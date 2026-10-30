#!/bin/busybox sh
export PATH=/bin
/bin/busybox --install -s /bin
set -eux
trap 'echo ASTER_KERNEL_PROBE_FAIL; poweroff -f' EXIT
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /dev
mknod /dev/console c 5 1
mknod /dev/null c 1 3
echo ASTER_KERNEL_PROBE_BEGIN
uname -a
mkdir /dev/binderfs
mount -t binder binder /dev/binderfs
test -c /dev/binderfs/binder-control
test -c /dev/binderfs/binder
test -c /dev/binderfs/hwbinder
test -c /dev/binderfs/vndbinder
ls -l /dev/binderfs
echo ASTER_BINDERFS_DEVICES_OK
mkdir /dev/selinux
mount -t selinuxfs selinuxfs /dev/selinux
test -f /dev/selinux/enforce
echo ASTER_SELINUXFS_OK
for module in virtio_mmio virtio_blk virtio-rng virtio_input; do
    insmod "/modules/$module.ko"
done
cat /proc/modules
test -d /sys/block/vda
device=$(cat /sys/block/vda/dev)
mknod /dev/vda b "${device%:*}" "${device#*:}"
dd if=/dev/vda bs=512 count=1 2>/dev/null | grep -q ASTER_ANDROID_KERNEL_BLOCK_PROBE
echo ASTER_VIRTIO_BLOCK_READ_OK
grep -q virtio_rng /sys/class/misc/hw_random/rng_current
grep -q 'QEMU Virtio Keyboard' /sys/class/input/input*/name
echo ASTER_VIRTIO_RNG_AND_INPUT_ENUMERATED
echo ASTER_KERNEL_PROBE_PASS
trap - EXIT
poweroff -f
