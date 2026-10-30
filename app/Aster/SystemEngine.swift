import Foundation
import UIKit

enum SystemEngine {
    @MainActor
    static func bootLinux() {
        UIApplication.shared.isIdleTimerDisabled = true
        Thread.detachNewThread {
            defer {
                DispatchQueue.main.async {
                    UIApplication.shared.isIdleTimerDisabled = false
                }
            }
            guard let kernel = Bundle.main.path(forResource: "vmlinuz-virt", ofType: nil),
                  let initramfs = Bundle.main.path(forResource: "initramfs-virt", ofType: nil) else {
                print("[linux] FAIL missing bundled Alpine images")
                return
            }
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let log = documents.appendingPathComponent("linux-serial.log")
            var result = aster_system_result()
            for limits: (UInt32, UInt32) in [(127, 180), (769, 180), (512, 0), (512, 601)] {
                guard aster_system_run(kernel, initramfs, log.path, limits.0, limits.1, &result) == -1 else {
                    print("[linux] summary FAIL invalid resource limits accepted")
                    return
                }
            }
            let args = CommandLine.arguments
            let runID = args.first(where: { $0.hasPrefix("--linux-run-id=") })?.dropFirst("--linux-run-id=".count) ?? "manual"
            let timeout: UInt32 = args.contains("--linux-timeout-test") ? 1 : 180
            if args.contains("--linux-cancel-test") {
                DispatchQueue.global().asyncAfter(deadline: .now() + 1) {
                    aster_system_cancel()
                }
            }
            print("[linux] starting QEMU TCI ram_mib=512 timeout_s=\(timeout) serial=\(log.path)")
            let status = aster_system_run(kernel, initramfs, log.path, 512, timeout, &result)
            var rejected = aster_system_result()
            let restartStatus = aster_system_run(kernel, initramfs, log.path, 512, timeout, &rejected)
            print("[linux] resource bounds rejected; restart_status=\(restartStatus)")
            print("[linux] status=\(status) userspace=\(result.reached_userspace) userspace_s=\(result.userspace_seconds) wall_s=\(result.wall_seconds) peak_rss_bytes=\(result.peak_rss_bytes) cpu_percent=\(result.cpu_percent) cancelled=\(result.cancelled) timeout=\(result.timed_out)")
            let passed = status == 0 && result.reached_userspace == 1 && result.timed_out == 0 && result.cancelled == 0 && restartStatus == -3
            let record: [String: Any] = [
                "status": status, "passed": passed, "userspace": result.reached_userspace,
                "userspace_seconds": result.userspace_seconds, "wall_seconds": result.wall_seconds,
                "peak_rss_bytes": result.peak_rss_bytes, "cpu_percent": result.cpu_percent,
                "cancelled": result.cancelled, "timed_out": result.timed_out,
                "ram_mib": 512, "timeout_seconds": timeout,
                "engine": "QEMU 11.1.3 TCI", "machine": "virt,gic-version=3",
                "run_id": String(runID),
                "restart_status": restartStatus,
            ]
            do {
                let json = try JSONSerialization.data(withJSONObject: record, options: [.prettyPrinted, .sortedKeys])
                try json.write(to: documents.appendingPathComponent("linux-result.json"), options: .atomic)
            } catch {
                print("[linux] FAIL could not persist measurement: \(error)")
                print("[linux] summary FAIL")
                return
            }
            print("[linux] summary \(passed ? "PASS" : "FAIL")")
        }
    }
}
