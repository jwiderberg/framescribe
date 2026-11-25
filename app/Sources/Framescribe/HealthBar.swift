import FramescribeCore
import SwiftUI

/// The compact status of the health checks at the top of the window, with what to do when something is missing.
struct HealthBar: View {
    @Bindable var model: AppModel
    @State private var showDetails = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                summary
                Spacer()
                if let report = model.health.report {
                    Button("Details") { showDetails.toggle() }
                        .popover(isPresented: $showDetails, arrowEdge: .bottom) { CheckList(report: report) }
                        .accessibilityLabel("Show every health check")
                }
                Button("Check Again") { Task { await model.checkHealth() } }
                    .disabled(model.health == .checking || model.download.isRunning)
            }
            if let report = model.health.report, !report.allowsJobs {
                problems(report)
            }
            if model.download.isRunning || model.download.progress.phase != .idle {
                DownloadProgressView(model: model)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    @ViewBuilder private var summary: some View {
        switch model.health {
        case .checking:
            ProgressView().controlSize(.small)
            Text("Checking ffmpeg, the speech model and the runtime…").foregroundStyle(.secondary)
        case let .unavailable(message):
            Image(systemName: "xmark.octagon.fill").foregroundStyle(.red).accessibilityHidden(true)
            Text(message).textSelection(.enabled)
        case let .checked(report):
            if report.allowsJobs {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).accessibilityHidden(true)
                Text(readyText(report)).foregroundStyle(.secondary)
            } else {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange).accessibilityHidden(true)
                Text("Something framescribe needs is missing. Videos you add wait until it is fixed.")
            }
        }
    }

    private func readyText(_ report: DoctorReport) -> String {
        var parts = ["Ready"]
        if let ffmpeg = report.checks.first(where: { $0.name == "ffmpeg" }) {
            parts.append("ffmpeg " + (ffmpeg.detail.split(separator: " ").first.map(String.init) ?? ""))
        }
        parts.append("speech model present")
        let warnings = report.warnings
        if !warnings.isEmpty { parts.append(warnings.map { "\($0.name): \($0.detail)" }.joined(separator: "; ")) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private func problems(_ report: DoctorReport) -> some View {
        if report.ffmpegMissing {
            VStack(alignment: .leading, spacing: 6) {
                Text("ffmpeg and ffprobe are needed and were not found.").font(.headline)
                Text("The app looked in /opt/homebrew/bin, /usr/local/bin and the PATH of your login shell. Install them with Homebrew (brew.sh), then choose Check Again:")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Text(DoctorReport.homebrewCommand)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(RoundedRectangle(cornerRadius: 5).fill(Color(nsColor: .textBackgroundColor)))
                    Button("Copy") { model.copy(DoctorReport.homebrewCommand) }
                        .accessibilityLabel("Copy the Homebrew command")
                }
            }
        }
        if report.modelMissing && !model.download.isRunning {
            VStack(alignment: .leading, spacing: 6) {
                Text("The speech model is not on this Mac yet.").font(.headline)
                Text("It is downloaded once from Hugging Face into its cache and then used offline. Nothing is downloaded until you click.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Download speech model (about 2.5 GB)") { model.downloadModel() }
                    .disabled(model.commands == nil)
            }
        }
        ForEach(report.otherBlocking) { check in
            VStack(alignment: .leading, spacing: 2) {
                Text("\(check.name): \(check.detail)").font(.headline).textSelection(.enabled)
                if let fix = check.fix { Text("Fix: \(fix)").foregroundStyle(.secondary).textSelection(.enabled) }
            }
        }
    }
}

struct DownloadProgressView: View {
    @Bindable var model: AppModel

    var body: some View {
        let p = model.download.progress
        VStack(alignment: .leading, spacing: 4) {
            switch p.phase {
            case .idle, .running:
                HStack {
                    if let f = p.fraction {
                        ProgressView(value: f)
                            .accessibilityLabel("Speech model download")
                            .accessibilityValue("\(Checked.percent(f)) percent")
                    } else {
                        ProgressView().progressViewStyle(.linear).accessibilityLabel("Speech model download")
                    }
                    Button("Stop") { model.download.stop() }
                        .accessibilityLabel("Stop the download")
                }
                Text(bytesText(p)).font(.caption).foregroundStyle(.secondary).monospacedDigit()
            case .finished:
                Label("Speech model downloaded.", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
            case let .failed(message):
                Label("The download stopped: \(message)", systemImage: "xmark.octagon")
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    private func bytesText(_ p: SetupProgress) -> String {
        let gb = { (b: Int64) in String(format: "%.2f GB", Double(b) / 1e9) }
        let pct = p.fraction.map { " (\(Checked.percent($0))%)" } ?? ""
        return "Downloading: \(gb(p.doneBytes)) of about \(gb(p.totalBytes))\(pct)"
    }
}

struct CheckList: View {
    let report: DoctorReport

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Health checks").font(.headline)
            ForEach(report.checks) { check in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: icon(check)).foregroundStyle(color(check)).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(check.name).fontWeight(.medium)
                        Text(check.detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        if let fix = check.fix, check.status != "ok" {
                            Text("Fix: \(fix)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(check.name), \(check.status): \(check.detail)")
            }
        }
        .padding(16)
        .frame(width: 460)
    }

    private func icon(_ c: DoctorCheck) -> String {
        c.status == "ok" ? "checkmark.circle.fill" : c.status == "warning" ? "exclamationmark.triangle.fill" : "xmark.octagon.fill"
    }

    private func color(_ c: DoctorCheck) -> Color {
        c.status == "ok" ? .green : c.status == "warning" ? .orange : .red
    }
}
