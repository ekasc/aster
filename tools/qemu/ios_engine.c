#include "qemu/osdep.h"
#include "qemu-main.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "system/system.h"
#include "system/runstate.h"
#include "system/cpus.h"
#include "hw/core/cpu.h"
#include "system/replay.h"
#include "aster/system_engine.h"
#include <stdatomic.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <poll.h>

static atomic_bool used, cancel_requested, userspace, vcpu_stopping, serial_done;
static QEMUTimer *poll_timer;
static int serial_fd, log_fd;
static double started, userspace_time;
static unsigned deadline;
static bool timed_out;
int (*qemu_main)(void);

bool aster_qemu_vcpu_stopping(void);
bool aster_qemu_vcpu_stopping(void)
{
    return atomic_load(&vcpu_stopping);
}

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static double cpu_seconds(struct rusage *r)
{
    return r->ru_utime.tv_sec + r->ru_utime.tv_usec / 1e6 +
           r->ru_stime.tv_sec + r->ru_stime.tv_usec / 1e6;
}

static bool write_all(int fd, const char *bytes, size_t count)
{
    while (count) {
        ssize_t written = write(fd, bytes, count);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return false;
        bytes += written;
        count -= written;
    }
    return true;
}

static void *read_serial(void *unused)
{
    char bytes[4096], recent[8192] = {0};
    size_t retained = 0;
    bool sent = false;
    ssize_t n;
    for (;;) {
        struct pollfd descriptor = { .fd = serial_fd, .events = POLLIN };
        int ready = poll(&descriptor, 1, 100);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) { atomic_store(&cancel_requested, true); break; }
        if (!ready) {
            if (atomic_load(&serial_done)) break;
            continue;
        }
        n = read(serial_fd, bytes, sizeof(bytes));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        if (!write_all(log_fd, bytes, n)) atomic_store(&cancel_requested, true);
        fwrite(bytes, 1, n, stdout);
        fflush(stdout);
        if (retained + n >= sizeof(recent)) {
            size_t keep = 1024;
            memmove(recent, recent + retained - keep, keep);
            retained = keep;
        }
        memcpy(recent + retained, bytes, n);
        retained += n;
        recent[retained] = 0;
        if (!sent && strstr(recent, "~ #")) {
            const char command[] = "echo ASTER-LINUX-$(echo USERSPACE); uname -a;\n";
            if (!write_all(serial_fd, command, sizeof(command) - 1))
                atomic_store(&cancel_requested, true);
            sent = true;
        }
        if (sent && strstr(recent, "\r\nASTER-LINUX-USERSPACE\r\n") &&
            !atomic_load(&userspace)) {
            userspace_time = now() - started;
            atomic_store(&userspace, true);
        }
    }
    return NULL;
}

static void poll_stop(void *unused)
{
    double elapsed = now() - started;
    timed_out = elapsed >= deadline;
    if (atomic_load(&cancel_requested) || timed_out ||
        (atomic_load(&userspace) && elapsed > userspace_time + 1)) {
        qemu_system_shutdown_request(SHUTDOWN_CAUSE_HOST_QMP_QUIT);
        return;
    }
    timer_mod(poll_timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 100);
}

void aster_system_cancel(void)
{
    atomic_store(&cancel_requested, true);
}

int aster_system_run(const char *kernel, const char *initramfs,
                     const char *log_path, unsigned ram_mib,
                     unsigned timeout_seconds, aster_system_result *result)
{
    if (!kernel || !initramfs || !log_path || !result || ram_mib < 128 ||
        ram_mib > 768 || timeout_seconds < 1 || timeout_seconds > 600) return -1;
    memset(result, 0, sizeof(*result));
    if (access(kernel, R_OK) || access(initramfs, R_OK)) return -2;
    if (atomic_exchange(&used, true)) return -3;
    log_fd = open(log_path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    int pair[2];
    if (log_fd < 0) return -4;
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair)) { close(log_fd); return -4; }
    serial_fd = pair[0];
    char memory[16], chardev[80];
    snprintf(memory, sizeof(memory), "%u", ram_mib);
    snprintf(chardev, sizeof(chardev), "socket,id=uart,fd=%d", pair[1]);
    const char *argv[] = {"aster-qemu", "-M", "virt,gic-version=3", "-cpu", "max",
        "-accel", "tcg,thread=single,tb-size=32", "-smp", "1", "-m", memory,
        "-display", "none", "-monitor", "none", "-nic", "none",
        "-chardev", chardev, "-serial", "chardev:uart", "-no-reboot",
        "-kernel", (char *)kernel, "-initrd", (char *)initramfs,
        "-append", "console=ttyAMA0", NULL};
    struct rusage before, after;
    getrusage(RUSAGE_SELF, &before);
    started = now();
    deadline = timeout_seconds;
    pthread_t reader;
    if (pthread_create(&reader, NULL, read_serial, NULL)) {
        close(pair[0]); close(pair[1]); close(log_fd); return -5;
    }
    qemu_init(G_N_ELEMENTS(argv) - 1, (char **)argv);
    poll_timer = timer_new_ms(QEMU_CLOCK_REALTIME, poll_stop, NULL);
    timer_mod(poll_timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME));
    int status = qemu_main_loop();
    timer_del(poll_timer);
    timer_free(poll_timer);
    qemu_cleanup(status);
    atomic_store(&vcpu_stopping, true);
    qemu_cpu_kick(first_cpu);
    bql_unlock();
    replay_mutex_unlock();
    qemu_thread_join(first_cpu->thread);
    fprintf(stdout, "[linux] vcpu joined\n");
    atomic_store(&serial_done, true);
    pthread_join(reader, NULL);
    close(pair[0]);
    if (fsync(log_fd)) atomic_store(&cancel_requested, true);
    close(log_fd);
    getrusage(RUSAGE_SELF, &after);
    result->wall_seconds = now() - started;
    result->userspace_seconds = userspace_time;
    result->cpu_percent = 100 * (cpu_seconds(&after) - cpu_seconds(&before)) / result->wall_seconds;
    result->peak_rss_bytes = after.ru_maxrss;
    result->reached_userspace = atomic_load(&userspace);
    result->cancelled = atomic_load(&cancel_requested);
    result->timed_out = timed_out;
    return status;
}
