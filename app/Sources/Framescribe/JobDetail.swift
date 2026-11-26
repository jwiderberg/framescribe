import FramescribeCore
import SwiftUI

/// One job: its stages while it runs, the results when it is done, the error when it failed.
struct JobDetail: View {
    @Bindable var model: AppModel
    let job: Job
    @State private var showLog = false
    @State private var outputSize: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                switch job.state {
                case .waiting: waiting
                case .running: running
                case .done: done
                case .failed: failed
                case .cancelled: cancelled
                }
                if job.state != .waiting { stageSection }
                if !job.progress.logLines.isEmpty { logSection }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // ---- header -----------------------------------------------------------------------------------------------

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(job.name).font(.title2.weight(.semibold)).lineLimit(2).textSelection(.enabled)
            HStack(spacing: 8) {
                Text(job.video.deletingLastPathComponent().path).lineLimit(1).truncationMode(.middle)
                if let d = job.duration { Text("· \(Clock.format(d))").accessibilityLabel("duration \(Clock.format(d))") }
            }
            .font(.callout)
            .foregroundStyle(.secondary)
            if let other = job.sharesOutputWith, !job.state.isFinished {
                Label("Another queued video has the same name (\(other)). Each gets its own output folder: the later one goes to a folder with a number added.",
                      systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // ---- per state --------------------------------------------------------------------------------------------

    private var waiting: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(waitingText).foregroundStyle(.secondary)
            Button("Remove from Queue") { model.queue.remove(job) }
        }
    }

    private var waitingText: String {
        if !model.queue.canStart { return "Waiting until the checks at the top pass." }
        if let running = model.queue.runningJob { return "Waiting for \(running.name) to finish. Videos are processed one at a time." }
        return "Waiting."
    }

    private var running: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text(job.progress.hasStarted ? job.progress.overallLine(now: context.date)
                         : "Starting… \(Clock.format(job.startedAt.map { context.date.timeIntervalSince($0) } ?? 0))")
                        .monospacedDigit()
                        .accessibilityLabel("Overall progress")
                        .accessibilityValue(job.progress.overallLine(now: context.date))
                    Spacer()
                    Button(job.stopRequested ? "Stopping…" : "Stop") { model.queue.stop(job) }
                        .disabled(job.stopRequested)
                        .accessibilityLabel("Stop processing this video")
                }
                if let silence = job.progress.silence(now: context.date), silence >= 15 {
                    Text("Still working: no update for \(Clock.format(silence)).")
                        .font(.callout).foregroundStyle(.secondary).monospacedDigit()
                }
                if let dir = job.runDirectory {
                    Text("Results go to \(dir.path)")
                        .font(.callout).foregroundStyle(.secondary)
                        .lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                }
            }
        }
    }

    @ViewBuilder private var done: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("The report is ready.", systemImage: "checkmark.circle.fill")
                .font(.headline)
                .foregroundStyle(.green)
            if let dir = job.runDirectory {
                Text(dir.path)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(2)
                    .truncationMode(.middle)
                if let notice = job.completenessNotice {
                    Label(notice, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout.weight(.medium))
                        .foregroundStyle(.orange)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.orange.opacity(0.12)))
                        .accessibilityLabel("Warning: \(notice)")
                }
                HStack {
                    Button("Open report") { if let r = job.report { model.open(r) } }
                        .keyboardShortcut(.defaultAction)
                        .accessibilityLabel("Open report.md in the default app")
                    Button("Show in Finder") { model.reveal(dir) }
                        .accessibilityLabel("Show the output folder in the Finder")
                    Button("Copy path") { model.copy(dir.path) }
                        .accessibilityLabel("Copy the output folder's path")
                }
            }
            overview
        }
        .task(id: job.runDirectory) {
            guard let dir = job.runDirectory else { return }
            let bytes = await Task.detached(priority: .utility) { RunOverview.folderBytes(dir) }.value
            outputSize = bytes.map(RunOverview.size)
        }
    }

    /// Where the time went and what came out. The stages' technical notes (which decode of a chunk was kept, where a
    /// cut fell) stay in the log: the view only counts them.
    @ViewBuilder private var overview: some View {
        let o = RunOverview(job.progress)
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
            ForEach(o.rows) { row in
                GridRow {
                    Text(row.label).foregroundStyle(.secondary)
                    Text(row.value).monospacedDigit().textSelection(.enabled)
                }
            }
            if let outputSize {
                GridRow {
                    Text("Output folder").foregroundStyle(.secondary)
                    Text(outputSize).monospacedDigit()
                }
            }
        }
        .font(.callout)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Overview of the run")
        if let c = o.completeness {
            VStack(alignment: .leading, spacing: 2) {
                Text("Completeness").font(.subheadline.weight(.semibold))
                Text(c).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
        }
        let n = job.progress.warnings.count
        if n > 0 {
            Text("\(n) technical \(n == 1 ? "note" : "notes") in the log.").font(.caption).foregroundStyle(.secondary)
        }
    }

    private var failed: some View {
        VStack(alignment: .leading, spacing: 10) {
            let failure = job.failure
            Label(failedTitle(failure), systemImage: "xmark.octagon.fill")
                .font(.headline)
                .foregroundStyle(.red)
            Text(failure?.message ?? "framescribe stopped without a message.")
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if case .crashed? = failure?.kind, let detail = failure?.detail {
                Text(detail).font(.system(.callout, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if failure?.kind == .general, let c = job.progress.completeness {
                Text(c.summary).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
            }
            HStack {
                if let log = failure?.logPath {
                    Button("Show log") { model.reveal(URL(fileURLWithPath: log)) }
                        .accessibilityLabel("Show the log file in the Finder")
                }
                if failure?.kind != .noAudio { Button("Try Again") { model.runAgain(job) } }
                Button("Remove") { model.queue.remove(job) }
            }
        }
    }

    private func failedTitle(_ failure: JobFailure?) -> String {
        switch failure?.kind {
        case .noAudio?: return "Nothing to transcribe."
        case .videoMissing?: return "The video is gone."
        case .incompleteTranscript?: return "The transcript was not kept."
        default: return failure?.stage.map { "Failed in stage \($0)." } ?? "Failed."
        }
    }

    private var cancelled: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Stopped.", systemImage: "stop.circle").font(.headline)
            Text("Finished stages stay cached: running it again continues from there.").foregroundStyle(.secondary)
            HStack {
                Button("Run Again") { model.runAgain(job) }
                Button("Remove") { model.queue.remove(job) }
            }
        }
    }

    // ---- stages and log -------------------------------------------------------------------------------------

    private var stageSection: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            VStack(alignment: .leading, spacing: 0) {
                ForEach(job.progress.stages) { stage in
                    StageRow(stage: stage, now: context.date)
                    Divider()
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Stages")
    }

    private var logSection: some View {
        DisclosureGroup(isExpanded: $showLog) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(job.progress.logLines.suffix(500)) { line in
                        Text("[\(Clock.format(line.t))] \(line.stage): \(line.message)")
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(6)
            }
            .frame(minHeight: 120, maxHeight: 240)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            if let log = job.appLog {
                Button("Show the full log in the Finder") { model.reveal(log) }.buttonStyle(.link)
            }
        } label: {
            Text("Log (\(job.progress.logLines.count + job.progress.droppedLogLines) lines)")
        }
        .accessibilityLabel("Log lines")
    }
}

struct StageRow: View {
    let stage: StageView
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                icon.frame(width: 16)
                Text(stage.name).fontWeight(stage.status == .running ? .semibold : .regular)
                Spacer()
                Text(trailing).foregroundStyle(.secondary).monospacedDigit()
            }
            if stage.status == .running {
                VStack(alignment: .leading, spacing: 3) {
                    if let f = stage.fraction {
                        HStack {
                            ProgressView(value: f)
                                .accessibilityLabel("\(stage.name) progress")
                                .accessibilityValue("\(Checked.percent(f)) percent")
                            Text("\(Checked.percent(f))%").monospacedDigit().frame(width: 44, alignment: .trailing)
                        }
                    } else {
                        ProgressView().progressViewStyle(.linear).accessibilityLabel("\(stage.name) progress")
                    }
                    Text(runningDetail).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
                .padding(.leading, 24)
            } else if let detail = detailText {
                Text(detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    .padding(.leading, 24)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var icon: some View {
        switch stage.status {
        case .pending: Image(systemName: "circle").foregroundStyle(.tertiary)
        case .running: Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(Color.accentColor)
        case .cached: Image(systemName: "archivebox").foregroundStyle(.blue)
        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .skipped: Image(systemName: "minus.circle").foregroundStyle(.secondary)
        case .failed: Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
        }
    }

    private var trailing: String {
        switch stage.status {
        case .pending: return "pending"
        case .running: return "running"
        case .cached: return "cached" + (stage.wallSeconds.map { " · \(String(format: "%.1f", $0)) s" } ?? "")
        case .done: return "done" + (stage.wallSeconds.map { " · \(Clock.format($0))" } ?? "")
        case .skipped: return "skipped"
        case .failed: return "failed"
        }
    }

    private var runningDetail: String {
        var parts: [String] = []
        if let note = stage.note, !note.isEmpty { parts.append(note) }
        if let amount = stage.amountText { parts.append(amount) }
        if let elapsed = stage.elapsed(now: now) { parts.append("elapsed \(Clock.format(elapsed))") }
        parts.append("ETA " + (stage.eta(now: now).map(Clock.format) ?? "–"))
        return parts.joined(separator: " · ")
    }

    private var detailText: String? {
        switch stage.status {
        case .skipped: return stage.reason
        // A failed stage's raw message is written for the command line. The job view above shows the plain one.
        case .failed: return nil
        case .done, .cached: return stage.summary.flatMap { $0.isEmpty ? nil : $0 }
        default: return nil
        }
    }
}
