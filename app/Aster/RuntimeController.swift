import Foundation
import UIKit

/// One guest payload and the result its run must produce.
struct Payload: Identifiable, Hashable {
    enum Expectation: Hashable {
        case exitZero(marker: String)
        case fault(Int32)
    }

    let id: String
    let title: String
    let detail: String
    let expectation: Expectation

    static let gate: [Payload] = [
        Payload(id: "hello", title: "Instruction + memory gate",
                detail: "AArch64 integer execution, data/BSS, unaligned access",
                expectation: .exitZero(marker: "ASTER-PHASE1 OK")),
        Payload(id: "demo", title: "Control flow + CRC32",
                detail: "Recursion, calls, sorting, byte/word memory traffic",
                expectation: .exitZero(marker: "ASTER-DEMO OK")),
        Payload(id: "threads", title: "Guest threads + exclusives",
                detail: "4 guest threads, LDXR/STXR atomic counter",
                expectation: .exitZero(marker: "ASTER-THREADS OK")),
        Payload(id: "fp", title: "Scalar floating point",
                detail: "FADD/FMUL/FDIV, FCVTZS conversions",
                expectation: .exitZero(marker: "ASTER-FP OK")),
        Payload(id: "fault_unmapped", title: "Unmapped access faults",
                detail: "Write to 0x1000 must produce an unmapped fault",
                expectation: .fault(faultCode(ASTER_FAULT_UNMAPPED))),
        Payload(id: "fault_rodata", title: "Read-only page faults",
                detail: "Write to .rodata must produce a permission fault",
                expectation: .fault(faultCode(ASTER_FAULT_PERMISSIONS))),
    ]
}

struct RunOutcome {
    let payload: Payload
    let ok: Bool
    let exitCode: Int32
    let faultKind: Int32
    let faultPC: UInt64
    let instructions: UInt64
    let wallNanos: UInt64
    let peakRSS: UInt64
    let message: String
    let console: String

    var passed: Bool {
        switch payload.expectation {
        case .exitZero(let marker):
            return ok && exitCode == 0 && console.contains(marker)
        case .fault(let kind):
            return !ok && faultKind == kind
        }
    }

    var statusLabel: String {
        switch payload.expectation {
        case .exitZero:
            return ok ? "exit \(exitCode)" : "fault \(faultName(faultKind))"
        case .fault:
            return ok ? "no fault" : faultName(faultKind)
        }
    }

    var millionInstructionsPerSecond: Double {
        guard wallNanos > 0 else { return 0 }
        return Double(instructions) / (Double(wallNanos) / 1_000.0)
    }
}

func faultCode(_ kind: aster_fault_kind) -> Int32 {
    Int32(kind.rawValue)
}

func faultName(_ kind: Int32) -> String {
    switch kind {
    case faultCode(ASTER_FAULT_UNMAPPED): return "unmapped"
    case faultCode(ASTER_FAULT_PERMISSIONS): return "permissions"
    case faultCode(ASTER_FAULT_UNDEFINED_INSTRUCTION): return "undefined instruction"
    case faultCode(ASTER_FAULT_HYPERCALL): return "bad hypercall"
    case faultCode(ASTER_FAULT_HOST_ABORT): return "host abort"
    default: return "kind \(kind)"
    }
}

/// Collects console bytes from the C callback. The callback runs on the guest
/// thread, so the buffer is lock-protected and drained on the main actor.
final class ConsoleBox: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = ""

    func append(_ text: String) {
        lock.lock()
        buffer += text
        lock.unlock()
    }

    func drain() -> String {
        lock.lock()
        defer { lock.unlock() }
        let text = buffer
        buffer = ""
        return text
    }
}

@MainActor
final class RuntimeController: ObservableObject, @unchecked Sendable {
    @Published private(set) var outcomes: [String: RunOutcome] = [:]
    @Published private(set) var console = ""
    @Published private(set) var isRunning = false
    @Published private(set) var completed = 0
    @Published private(set) var summary = ""
    private var didAutoRun = false

    struct ExecutionPolicy {
        let mapJITResult: Int32
        let mapJITErrno: Int32
        let rwxResult: Int32
        let rwxErrno: Int32
        let rwThenRXResult: Int32
        let rwThenRXErrno: Int32
        let hostPageSize: Int32

        var mapJITDenied: Bool { mapJITResult != 0 }
        var rwxDenied: Bool { rwxResult != 0 }
        var rwThenRXAllowed: Bool { rwThenRXResult == 0 }

        func describe(allowed: Bool, errno: Int32) -> String {
            if allowed { return "allowed" }
            return "denied (errno \(errno))"
        }
    }

    let execPolicy: ExecutionPolicy = {
        var probe = aster_exec_policy()
        aster_probe_execution_policy(&probe)
        return ExecutionPolicy(mapJITResult: probe.map_jit_result,
                               mapJITErrno: probe.map_jit_errno,
                               rwxResult: probe.rwx_result,
                               rwxErrno: probe.rwx_errno,
                               rwThenRXResult: probe.rw_then_rx_result,
                               rwThenRXErrno: probe.rw_then_rx_errno,
                               hostPageSize: Int32(probe.host_page_size))
    }()

    let engineName = String(cString: aster_engine_name())
    let guestPageSize = aster_guest_page_size()
    let hostPageSize = aster_host_page_size()

    var deviceModel: String {
        var info = utsname()
        uname(&info)
        return withUnsafeBytes(of: &info.machine) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }
    }

    var totalRAM: String {
        ByteCountFormatter.string(fromByteCount: Int64(ProcessInfo.processInfo.physicalMemory),
                                  countStyle: .memory)
    }

    func logRuntimeIdentity() {
        print("[aster] engine=\(engineName) guest_page=\(guestPageSize) host_page=\(hostPageSize) device=\(deviceModel) os=\(UIDevice.current.systemVersion) ram=\(totalRAM)")
        print("[policy] map_jit=\(execPolicy.describe(allowed: !execPolicy.mapJITDenied, errno: execPolicy.mapJITErrno)) rwx=\(execPolicy.describe(allowed: !execPolicy.rwxDenied, errno: execPolicy.rwxErrno)) rw_to_rx=\(execPolicy.describe(allowed: execPolicy.rwThenRXAllowed, errno: execPolicy.rwThenRXErrno))")
    }

    /// Executes an unsigned RX page. If iOS enforces code signing at fault
    /// time, this call does not return and the process is terminated; the
    /// absence of a printed result is the measurement.
    func probeUnsignedExecution() {
        print("[probe] calling an unsigned RX page (mov w0,#1; ret)")
        let result = aster_probe_unsigned_execution()
        print("[probe] returned \(result) — unsigned execution was permitted")
    }

    func runGateOnce() {
        guard !didAutoRun else { return }
        didAutoRun = true
        runGate()
    }

    func runGate() {
        guard !isRunning else { return }
        isRunning = true
        outcomes = [:]
        console = ""
        completed = 0
        summary = ""
        let payloads = Payload.gate

        Task.detached(priority: .userInitiated) { [weak self] in
            for payload in payloads {
                let outcome = Self.run(payload)
                await self?.accept(outcome)
            }
            await self?.finish(total: payloads.count)
        }
    }

    private func accept(_ outcome: RunOutcome) {
        outcomes[outcome.payload.id] = outcome
        console += outcome.console
        completed += 1
        print(String(format: "[gate] %@ %@ status=%@ exit=%d fault=%d insns=%llu wall_ms=%.2f mips=%.1f peak_mb=%.1f msg=%@",
                     outcome.payload.id,
                     outcome.passed ? "PASS" : "FAIL",
                     outcome.statusLabel,
                     outcome.exitCode,
                     outcome.faultKind,
                     outcome.instructions,
                     Double(outcome.wallNanos) / 1_000_000,
                     outcome.millionInstructionsPerSecond,
                     Double(outcome.peakRSS) / (1024 * 1024),
                     outcome.message))
    }

    private func finish(total: Int) {
        let passed = outcomes.values.filter(\.passed).count
        summary = "\(passed)/\(total) checks passed"
        isRunning = false
        print("[gate] summary \(passed)/\(total)")
    }

    private nonisolated static func run(_ payload: Payload) -> RunOutcome {
        let box = ConsoleBox()
        let user = Unmanaged.passUnretained(box).toOpaque()
        let callback: aster_console_callback = { bytes, length, opaque in
            guard let bytes, let opaque else { return }
            let box = Unmanaged<ConsoleBox>.fromOpaque(opaque).takeUnretainedValue()
            box.append(String(decoding: UnsafeRawBufferPointer(start: bytes, count: length),
                              as: UTF8.self))
        }

        var options = aster_options()
        aster_default_options(&options)
        options.ram_size = 256 * 1024 * 1024
        options.cpu_count = 4
        options.console = callback
        options.user = user

        guard let url = Bundle.main.url(forResource: payload.id, withExtension: "elf"),
              let data = try? Data(contentsOf: url) else {
            return RunOutcome(payload: payload, ok: false, exitCode: -1,
                              faultKind: faultCode(ASTER_FAULT_HOST_ABORT), faultPC: 0,
                              instructions: 0, wallNanos: 0, peakRSS: 0,
                              message: "payload \(payload.id).elf missing from bundle",
                              console: "")
        }

        var result = aster_result()
        let status = data.withUnsafeBytes { raw -> Int32 in
            let base = raw.bindMemory(to: UInt8.self).baseAddress
            return aster_run_elf(base, raw.count, &options, &result)
        }

        let message = withUnsafeBytes(of: &result.message) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }

        return RunOutcome(payload: payload,
                          ok: result.ok != 0,
                          exitCode: result.exit_code,
                          faultKind: result.fault_kind,
                          faultPC: result.fault_pc,
                          instructions: result.instructions,
                          wallNanos: result.wall_ns,
                          peakRSS: result.peak_rss,
                          message: "\(message) [status \(status)]",
                          console: box.drain())
    }
}
