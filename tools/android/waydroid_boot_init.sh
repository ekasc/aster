#!/bin/sh
# The pinned GKI has CONFIG_PID_NS=n. Replace guest PID 1 rather than trying
# unshare -pf. System/vendor remain read-only; all writes die with the VM.
# Source comparison and real failure logs are in docs/phase3-waydroid.md.

PATH=/sbin:/bin:/usr/sbin:/usr/bin
export PATH

ROOT=/android
SYSIMG=/mnt/system-ro
RW=/mnt/root-rw
LIMIT=300

log() {
    printf 'ASTER-WAYDROID|%s\n' "$*"
}

short() {
    printf '%s' "$1" | tr '\n' ' ' | cut -c1-110
}

try_mount() {
    # try_mount <options> <fstype> <source> <target> <tag>
    mkdir -p "$4"
    detail=$(mount -t "$2" -o "$1" "$3" "$4" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log "$5=ok"
    else
        log "$5=failed rc=$rc detail=$(short "$detail")"
    fi
    return "$rc"
}

abort() {
    # The transcript parser treats a failing android_init key as blocking.
    log "android_init=failed reason=$1"
    poweroff -f
    exit 1
}

mknod /dev/console c 5 1 2>/dev/null
mknod /dev/null c 1 3 2>/dev/null
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null

log "kernel=$(uname -r)"
log "uptime=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
log "cmdline=$(short "$(cat /proc/cmdline 2>/dev/null)")"

for module in virtio_pci_modern_dev virtio_pci_legacy_dev virtio_pci virtio_blk; do
    if [ ! -f "/lib/modules/$module.ko" ]; then
        log "insmod=$module absent"
        continue
    fi
    detail=$(insmod "/lib/modules/$module.ko" 2>&1)
    log "insmod=$module rc=$? detail=$(short "$detail")"
done

for block in /sys/class/block/vd*; do
    [ -r "$block/dev" ] || continue
    name=${block##*/}
    major=$(cut -d: -f1 "$block/dev")
    minor=$(cut -d: -f2 "$block/dev")
    if mknod "/dev/$name" b "$major" "$minor" 2>/dev/null; then
        log "device=/dev/$name present=yes major=$major minor=$minor"
    else
        log "device=/dev/$name present=no create=failed"
    fi
done

log "memtotal_kb=$(sed -n 's/^MemTotal: *\([0-9]*\).*/\1/p' /proc/meminfo 2>/dev/null)"

mkdir -p "$SYSIMG" "$RW" "$ROOT"
try_mount ro ext4 /dev/vda "$SYSIMG" system_mount || abort no_system_image
try_mount "size=768m,mode=0755" tmpfs tmpfs "$RW" root_tmpfs || abort no_tmpfs
# These must exist inside the tmpfs, not underneath it.
mkdir -p "$RW/upper" "$RW/work"

overlay_opts="lowerdir=$SYSIMG,upperdir=$RW/upper,workdir=$RW/work"
# Upstream adds xino=off on kernels >= 4.17 (mount.py:168). This kernel is built
# with "# CONFIG_OVERLAY_FS_XINO_AUTO is not set", so xino cannot be on and the
# option is left out rather than risk an unknown-option EINVAL.
if try_mount "$overlay_opts" overlay overlay "$ROOT" root_overlay; then
    log "root_mode=overlay"
else
    # Degraded path: a read-only root can still start first-stage init, and the
    # serial log will name whatever then fails.
    log "root_mode=ro_bind"
    if ! detail=$(mount -o bind "$SYSIMG" "$ROOT" 2>&1); then
        log "root_bind=failed detail=$(short "$detail")"
        abort no_root
    fi
    mount -o remount,ro "$ROOT" 2>/dev/null
    log "root_bind_ro=$(short "$(grep -m1 " $ROOT " /proc/mounts 2>/dev/null)")"
fi

try_mount ro ext4 /dev/vdb "$ROOT/vendor" vendor_mount || abort no_vendor_image
# Ephemeral, isolated userdata: no host directory and no image is shared.
try_mount "size=1024m,mode=0771" tmpfs tmpfs "$ROOT/data" data_tmpfs || abort no_userdata

mkdir -p "$ROOT/dev" "$ROOT/proc"
try_mount "mode=0755" tmpfs tmpfs "$ROOT/dev" dev_tmpfs || abort no_dev
try_mount defaults proc proc "$ROOT/proc" proc_mount || abort no_proc
# FirstStageMain creates its own nodes. Existing kmsg/urandom are fatal EEXIST.
try_mount ro sysfs sysfs "$ROOT/sys" sys_mount || abort no_sysfs
try_mount defaults binder binder "$ROOT/dev/binderfs" binder_mount || abort no_binderfs
for node in binder hwbinder vndbinder; do
    [ -c "$ROOT/dev/binderfs/$node" ] || abort "missing_$node"
    chmod 0666 "$ROOT/dev/binderfs/$node" || abort "permissions_$node"
    ln -s "binderfs/$node" "$ROOT/dev/$node" || abort "link_$node"
done
log "binder_devices=binder hwbinder vndbinder"

for mnt in "$SYSIMG" "$RW" "$ROOT" "$ROOT/vendor" "$ROOT/data" "$ROOT/dev" "$ROOT/proc"; do
    log "mount=$mnt entry=$(short "$(grep -m1 " $mnt " /proc/mounts 2>/dev/null)")"
done
log "system_ro_proof=$(short "$(grep -m1 " $SYSIMG " /proc/mounts 2>/dev/null)")"
log "mounts_total=$(wc -l < /proc/mounts 2>/dev/null)"

log "ns_dir=$(ls /proc/self/ns 2>/dev/null | tr '\n' ' ')"
log "ns_pid=$([ -e /proc/self/ns/pid ] && echo present || echo absent_config_pid_ns_off)"
log "self_status=$(short "$(grep -E '^(CapEff|CapBnd)' /proc/self/status 2>/dev/null)")"
log "nspid=$(short "$(sed -n 's/^NSpid:[[:space:]]*//p' /proc/self/status 2>/dev/null)")"

if command -v unshare >/dev/null 2>&1; then
    out=$(unshare -pf true 2>&1)
    rc=$?
    log "probe_pidns=rc=$rc detail=$(short "$out")"
    out=$(unshare -m true 2>&1)
    rc=$?
    log "probe_mountns=rc=$rc detail=$(short "$out")"
else
    log "probe_pidns=unshare_missing"
fi

for node in console null zero urandom kmsg ashmem binder; do
    if [ -e "$ROOT/dev/$node" ]; then
        log "devnode=$node $(short "$(ls -l "$ROOT/dev/$node" 2>/dev/null)")"
    else
        log "devnode=$node absent"
    fi
done
log "binder_sysfs=$(short "$(cat /sys/class/misc/binder/dev 2>/dev/null)")"
log "binderfs_sysfs=$([ -d /sys/fs/binder ] && echo present || echo absent)"

if [ -L "$ROOT/init" ]; then
    log "init_link=$(readlink "$ROOT/init" 2>/dev/null)"
else
    log "init_link=not_a_symlink"
fi
log "init_bin=$([ -f "$ROOT/system/bin/init" ] && echo present || echo absent)"
if command -v od >/dev/null 2>&1; then
    log "init_elf=$(short "$(od -An -c -N20 "$ROOT/system/bin/init" 2>/dev/null)")"
fi
log "init_exe=$([ -x "$ROOT/system/bin/init" ] && echo yes || echo no)"

for prop in ro.build.version.release ro.build.version.sdk ro.product.cpu.abi ro.build.fingerprint; do
    value=$(sed -n "s/^$prop=//p" "$ROOT/system/build.prop" 2>/dev/null | head -1)
    log "image_property=$prop value=${value:-absent}"
done
log "vendor_waydroid_prop=$([ -f "$ROOT/vendor/waydroid.prop" ] && echo present || echo absent)"
log "selinux_enforce=$(cat /sys/fs/selinux/enforce 2>/dev/null || echo unset_preinit)"

from_cmdline=$(sed -n 's/.*aster\.guest_limit=\([0-9][0-9]*\).*/\1/p' /proc/cmdline 2>/dev/null | head -1)
[ -n "$from_cmdline" ] && LIMIT=$from_cmdline
log "guest_limit_seconds=$LIMIT"

if command -v sleep >/dev/null 2>&1 && command -v poweroff >/dev/null 2>&1; then
    (
        elapsed=0
        while [ "$elapsed" -lt "$LIMIT" ]; do
            sleep 30 || break
            elapsed=$((elapsed + 30))
            log "watchdog=alive elapsed=${elapsed}s init_comm=$(cat /proc/1/comm 2>/dev/null) procs=$(ls /proc 2>/dev/null | grep -c '^[0-9]')"
            value=$(timeout 3 chroot "$ROOT" /system/bin/getprop sys.boot_completed 2>/dev/null)
            log "prop=sys.boot_completed=$value"
            value=$(timeout 3 chroot "$ROOT" /system/bin/getprop ro.build.version.sdk 2>/dev/null)
            log "prop=ro.build.version.sdk=$value"
            timeout 3 chroot "$ROOT" /system/bin/setprop debug.aster.bootstrap ready 2>/dev/null
            log "property_write_rc=$?"
            value=$(timeout 3 chroot "$ROOT" /system/bin/getprop debug.aster.bootstrap 2>/dev/null)
            log "prop=debug.aster.bootstrap=$value"
            value=$(timeout 3 chroot "$ROOT" /system/bin/service list 2>&1)
            printf '%s\n' "$value" | while IFS= read -r line; do
                log "binder_services=$line"
            done
            for service in activity package; do
                value=$(timeout 3 chroot "$ROOT" /system/bin/service check "$service" 2>&1)
                log "service_$service=rc=$? detail=$(short "$value")"
            done
        done
        log "watchdog=expired limit=${LIMIT}s init_comm=$(cat /proc/1/comm 2>/dev/null)"
        poweroff -f
    ) &
    log "watchdog=armed pid=$!"
else
    log "watchdog=unavailable"
fi

# exec is required: a plain command would fork, leaving init as PID 2 under this
# shell. Note that "$ROOT/init" cannot be tested with -e from outside the chroot
# because its target is the absolute path /system/bin/init.
command -v chroot >/dev/null 2>&1 || abort chroot_missing
[ -x "$ROOT/system/bin/init" ] || abort init_binary_missing
log "init_start=$(date +%s)"
log "exec=chroot root=$ROOT argv=/init"
exec chroot "$ROOT" /init
