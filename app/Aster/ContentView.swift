import SwiftUI

struct ContentView: View {
    @StateObject private var runtime = RuntimeController()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    runtimeCard
                    policyCard
                    gateCard
                    consoleCard
                }
                .padding()
            }
            .background(Color.black)
            .navigationTitle("Aster")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text("Aster")
                        .font(.system(.headline, design: .monospaced))
                }
            }
        }
        .tint(.green)
        .task {
            runtime.logRuntimeIdentity()
            if CommandLine.arguments.contains("--probe-exec") {
                runtime.probeUnsignedExecution()
            } else if CommandLine.arguments.contains("--autorun") {
                runtime.runGateOnce()
            }
        }
    }

    private var runtimeCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("RUNTIME")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
            metric("Engine", runtime.engineName, monospaced: false)
            metric("Device", runtime.deviceModel)
            metric("Guest pages", "\(runtime.guestPageSize) B")
            metric("Host pages", "\(runtime.hostPageSize) B")
            metric("Physical RAM", runtime.totalRAM)
            metric("JIT", "unavailable on stock iOS")
        }
        .card()
    }

    private var policyCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("EXECUTION POLICY · MEASURED")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
            metric("MAP_JIT", runtime.execPolicy.describe(allowed: !runtime.execPolicy.mapJITDenied,
                                                          errno: runtime.execPolicy.mapJITErrno))
            metric("mmap RWX", runtime.execPolicy.describe(allowed: !runtime.execPolicy.rwxDenied,
                                                           errno: runtime.execPolicy.rwxErrno))
            metric("RW → RX", runtime.execPolicy.describe(allowed: runtime.execPolicy.rwThenRXAllowed,
                                                          errno: runtime.execPolicy.rwThenRXErrno))
            Text("Mappings are probed, never executed. A successful RW → RX mprotect is not proof that execution is permitted: unsigned pages still fail code-signature validation at fault time.")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
        .card()
    }

    private var gateCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("PHASE 1 GATE")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                if runtime.isRunning {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            ForEach(Payload.gate) { payload in
                payloadRow(payload)
                if payload.id != Payload.gate.last?.id {
                    Divider().overlay(Color.white.opacity(0.08))
                }
            }

            Button {
                runtime.runGate()
            } label: {
                Text(runtime.isRunning ? "Running" : "Run gate")
                    .font(.system(.subheadline, design: .monospaced).weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .disabled(runtime.isRunning)
            .accessibilityLabel("Run Phase 1 gate")
            .accessibilityHint("Executes six AArch64 guest payloads on this device")

            if !runtime.summary.isEmpty {
                Text(runtime.summary)
                    .font(.system(.footnote, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Gate result: \(runtime.summary)")
            }
        }
        .card()
    }

    private var consoleCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("CONSOLE")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
            ScrollView(.vertical) {
                Text(runtime.console.isEmpty ? "No guest output yet." : runtime.console)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(runtime.console.isEmpty ? .secondary : .primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(maxHeight: 280)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Guest console output")
        }
        .card()
    }

    @ViewBuilder
    private func payloadRow(_ payload: Payload) -> some View {
        let outcome = runtime.outcomes[payload.id]
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: statusSymbol(outcome))
                .foregroundStyle(statusColor(outcome))
                .font(.system(size: 15, weight: .semibold))
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(payload.title)
                    .font(.system(.subheadline, design: .monospaced).weight(.medium))
                Text(payload.detail)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                if let outcome {
                    Text(metricsLine(outcome))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 0)
            if let outcome {
                Text(outcome.statusLabel)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(statusColor(outcome))
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel(payload, outcome))
    }

    private func metricsLine(_ outcome: RunOutcome) -> String {
        let ms = Double(outcome.wallNanos) / 1_000_000
        let ips = outcome.millionInstructionsPerSecond
        let rss = Double(outcome.peakRSS) / (1024 * 1024)
        let instr = outcome.instructions >= 1_000_000
            ? String(format: "%.2f M", Double(outcome.instructions) / 1_000_000)
            : String(format: "%.0f k", Double(outcome.instructions) / 1_000)
        return String(format: "%@ instr · %.1f ms · %.0f MIPS · %.0f MB",
                      instr, ms, ips, rss)
    }

    private func accessibilityLabel(_ payload: Payload, _ outcome: RunOutcome?) -> String {
        guard let outcome else { return "\(payload.title). Not run." }
        let verdict = outcome.passed ? "passed" : "failed"
        return "\(payload.title). \(verdict). \(outcome.statusLabel). \(metricsLine(outcome))."
    }

    private func statusSymbol(_ outcome: RunOutcome?) -> String {
        guard let outcome else { return "circle.dashed" }
        return outcome.passed ? "checkmark.circle.fill" : "xmark.octagon.fill"
    }

    private func statusColor(_ outcome: RunOutcome?) -> Color {
        guard let outcome else { return .secondary }
        return outcome.passed ? .green : .red
    }

    private func metric(_ label: String, _ value: String, monospaced: Bool = true) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 106, alignment: .leading)
            Text(value)
                .font(monospaced ? .system(.caption, design: .monospaced)
                                 : .system(.caption))
                .foregroundStyle(.primary)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }
}

private extension View {
    func card() -> some View {
        padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color.white.opacity(0.04))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Color.white.opacity(0.08), lineWidth: 1)
            )
    }
}
