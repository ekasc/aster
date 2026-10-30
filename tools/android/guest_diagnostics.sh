#!/system/bin/sh
# ASTER guest boot diagnostics: one bounded snapshot from the existing local
# userdebug root console. This reports observations; it never declares a boot
# pass and never stops a guest process.
#
# Usage inside the guest (root console):
#   su 0 /system/bin/sh /data/local/tmp/guest_diagnostics.sh
#
# Output is ASTER-DIAG lines on stdout and nothing else. The lead schedules
# snapshots; this script has no loop and returns after one pass.
#
# ASTER_DIAG_PROC_ROOT overrides the process tree only so local fixture tests
# can read synthetic /proc data. Leave it unset in a real guest.
#
# Only system_server, zygote, and SurfaceFlinger are inspected. No app data,
# credentials, or /data paths are read.

set -u

proc_root=${ASTER_DIAG_PROC_ROOT:-/proc}
service_seconds=5
backtrace_seconds=20
backtrace_limit=200

have() {
    command -v "$1" >/dev/null 2>&1
}

if ! have getprop; then
    printf 'guest_diagnostics: getprop unavailable; cannot bind nonce\n' >&2
    exit 2
fi
nonce=$(getprop ro.boot.aster_nonce 2>/dev/null)
case "$nonce" in
    ''|*[!0-9a-f]*)
        printf 'guest_diagnostics: invalid ro.boot.aster_nonce\n' >&2
        exit 2
        ;;
esac
if [ "${#nonce}" != 32 ]; then
    printf 'guest_diagnostics: invalid ro.boot.aster_nonce\n' >&2
    exit 2
fi

sequence=0
emit() {
    sequence=$((sequence + 1))
    printf 'ASTER-DIAG|%s|%s|%s|%s\n' "$nonce" "$sequence" "$1" "$2"
}

clean() {
    printf '%s' "$1" | tr '\r\n|' ' ' | cut -c1-240
}

emit_clean() {
    emit "$1" "$(clean "$2")"
}

snapshot_prop() {
    prop_name=$1
    prop_value=$(getprop "$prop_name" 2>/dev/null)
    if [ -z "$prop_value" ]; then
        prop_value=empty
    fi
    emit_clean PROP "$prop_name=$prop_value"
}

snapshot_service() {
    service_name=$1
    if ! have service; then
        emit BINDER_SERVICE "$service_name tool=service_unavailable"
        return
    fi
    if have timeout; then
        service_output=$(timeout "$service_seconds" service check "$service_name" 2>&1)
        service_rc=$?
        service_bound=timeout
    else
        service_output=$(service check "$service_name" 2>&1)
        service_rc=$?
        service_bound=unbounded
    fi
    if [ -z "$service_output" ]; then
        service_output=empty
    fi
    emit_clean BINDER_SERVICE \
        "$service_name rc=$service_rc bound=$service_bound $service_output"
}

snapshot_process() {
    process_name=$1
    if ! have pidof; then
        emit PROCESS "$process_name tool=pidof_unavailable"
        return
    fi
    process_raw=$(pidof "$process_name" 2>/dev/null)
    process_pid=
    for process_token in $process_raw; do
        process_pid=$process_token
        break
    done
    if [ -z "$process_pid" ]; then
        emit PROCESS "$process_name absent"
        return
    fi
    case "$process_pid" in
        *[!0-9]*|0)
            emit_clean PROCESS "$process_name invalid_pid=$process_pid"
            return
            ;;
    esac
    if [ "$process_name" = system_server ]; then
        system_server_pid=$process_pid
    fi
    if [ ! -r "$proc_root/$process_pid/status" ]; then
        emit PROCESS "$process_name pid=$process_pid status_unreadable"
        return
    fi
    emit PROCESS "$process_name pid=$process_pid"

    process_status=
    while IFS= read -r process_line; do
        case "$process_line" in
            Name:*|State:*|Tgid:*|Pid:*|PPid:*|Threads:*|VmPeak:*|VmSize:*|VmRSS:*|VmSwap:*)
                process_key=${process_line%%:*}
                process_value=${process_line#*:}
                while :; do
                    case "$process_value" in
                        [[:space:]]*) process_value=${process_value#?} ;;
                        *) break ;;
                    esac
                done
                process_status="$process_status $process_key=$process_value"
                ;;
        esac
    done < "$proc_root/$process_pid/status"
    if [ -z "$process_status" ]; then
        emit PROCESS_STATUS "$process_name status_fields_unavailable"
    else
        emit_clean PROCESS_STATUS "$process_name$process_status"
    fi

    if [ -r "$proc_root/$process_pid/wchan" ]; then
        process_wchan=
        read -r process_wchan < "$proc_root/$process_pid/wchan" 2>/dev/null || true
        if [ -z "$process_wchan" ]; then
            process_wchan=empty
        fi
    else
        process_wchan=unreadable
    fi
    emit_clean PROCESS_WCHAN "$process_name=$process_wchan"

    if have ps; then
        process_ps=$(ps -p "$process_pid" -o PID,PPID,STAT,WCHAN,VSZ,RSS,NAME 2>&1)
        process_ps_rc=$?
        if [ "$process_ps_rc" -ne 0 ] || [ -z "$process_ps" ]; then
            emit PS_ROW "$process_name rc=$process_ps_rc unavailable"
        else
            emit_clean PS_ROW "$process_name $process_ps"
        fi
    else
        emit PS_ROW "$process_name tool=ps_unavailable"
    fi
}

snapshot_backtrace() {
    backtrace_pid=$1
    if ! have timeout; then
        emit BACKTRACE "system_server pid=$backtrace_pid skipped timeout_unavailable"
        return
    fi
    if ! have debuggerd; then
        emit BACKTRACE "system_server pid=$backtrace_pid skipped debuggerd_unavailable"
        return
    fi
    backtrace_output=$(timeout "$backtrace_seconds" debuggerd -b "$backtrace_pid" 2>&1)
    backtrace_rc=$?
    backtrace_lines=0
    backtrace_truncated=0
    saved_ifs=$IFS
    IFS='
'
    set -f
    for backtrace_line in $backtrace_output; do
        if [ "$backtrace_lines" -ge "$backtrace_limit" ]; then
            backtrace_truncated=1
            break
        fi
        backtrace_lines=$((backtrace_lines + 1))
        emit_clean BACKTRACE_LINE "$backtrace_line"
    done
    set +f
    IFS=$saved_ifs
    emit BACKTRACE \
        "system_server pid=$backtrace_pid rc=$backtrace_rc lines=$backtrace_lines limit=$backtrace_limit truncated=$backtrace_truncated"
}

emit SNAPSHOT guest_diagnostics_v1

for tool in pidof ps service timeout debuggerd; do
    if have "$tool"; then
        emit TOOL "$tool=available"
    else
        emit TOOL "$tool=unavailable"
    fi
done

uptime_value=unknown
if [ -r "$proc_root/uptime" ]; then
    uptime_line=
    read -r uptime_line _ < "$proc_root/uptime" 2>/dev/null || true
    if [ -n "$uptime_line" ]; then
        uptime_value=$uptime_line
    fi
fi
emit UPTIME_SECONDS "$uptime_value"

boot_completed=$(getprop sys.boot_completed 2>/dev/null)
if [ -z "$boot_completed" ]; then
    boot_completed=empty
fi
emit_clean BOOT_COMPLETED "$boot_completed"

snapshot_prop init.svc.zygote
snapshot_prop init.svc.zygote_secondary
snapshot_prop init.svc.surfaceflinger
snapshot_prop init.svc.servicemanager
snapshot_prop init.svc.logd
snapshot_prop init.svc.bootanim

snapshot_service activity
snapshot_service package
snapshot_service window
snapshot_service SurfaceFlinger

system_server_pid=
snapshot_process system_server
snapshot_process zygote
snapshot_process zygote_secondary
snapshot_process surfaceflinger

if [ -n "$system_server_pid" ]; then
    snapshot_backtrace "$system_server_pid"
else
    emit BACKTRACE "system_server skipped absent"
fi

emit SNAPSHOT_END guest_diagnostics_v1
exit 0
