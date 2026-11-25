import FramescribeCore
import SwiftUI

struct ContentView: View {
    @Bindable var model: AppModel
    @State private var dropTargeted = false
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(spacing: 0) {
            HealthBar(model: model)
            Divider()
            if let notice = model.notice {
                NoticeBar(notice: notice) { model.notice = nil }
                Divider()
            }
            if model.queue.jobs.isEmpty {
                DropZone(targeted: dropTargeted, choose: model.chooseVideos)
                    .padding(24)
            } else {
                HSplitView {
                    QueueList(model: model)
                        .frame(minWidth: 220, idealWidth: 260, maxWidth: 360)
                    Group {
                        if let job = model.selectedJob {
                            JobDetail(model: model, job: job)
                        } else {
                            DropZone(targeted: dropTargeted, choose: model.chooseVideos).padding(24)
                        }
                    }
                    .frame(minWidth: 380, maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            model.add(urls)
            return true
        } isTargeted: { dropTargeted = $0 }
        .onAppear { model.showMainWindow = { [openWindow] in openWindow(id: "main") } }
        // the notice stays until dismissed or until a video is added: one sent at launch must not vanish unseen
    }
}

// ---- drop zone --------------------------------------------------------------------------------------------------

struct DropZone: View {
    let targeted: Bool
    let choose: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "film.stack")
                .font(.system(size: 52, weight: .light))
                .foregroundStyle(targeted ? Color.accentColor : .secondary)
                .accessibilityHidden(true)
            Text("Drop a screen recording here")
                .font(.title2.weight(.semibold))
            Text("Framescribe turns it into a report with the transcript, key frames and on-screen text.\nYou can also drop videos on the app's icon, or choose File ▸ Open… (⌘O).")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button("Choose Video…", action: choose)
                .controlSize(.large)
                .accessibilityLabel("Choose a video to process")
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background {
            RoundedRectangle(cornerRadius: 14)
                .strokeBorder(style: StrokeStyle(lineWidth: 2, dash: [8, 6]))
                .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.5))
                .background(RoundedRectangle(cornerRadius: 14)
                    .fill(targeted ? Color.accentColor.opacity(0.08) : Color.clear))
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Drop zone for videos")
    }
}

struct NoticeBar: View {
    let notice: Notice
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.yellow)
                .accessibilityHidden(true)
            Text(notice.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) { Image(systemName: "xmark") }
                .buttonStyle(.borderless)
                .accessibilityLabel("Dismiss message")
                .help("Dismiss")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.yellow.opacity(0.12))
        .accessibilityElement(children: .contain)  // .combine would swallow the dismiss button's label
    }
}

// ---- queue --------------------------------------------------------------------------------------------------------

struct QueueList: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            List(selection: $model.selectedJobID) {
                ForEach(model.queue.jobs) { job in
                    JobRow(job: job, canStart: model.queue.canStart)
                        .tag(job.id)
                        .contextMenu {
                            if job.state == .running {
                                Button("Stop") { model.queue.stop(job) }
                            } else {
                                Button("Remove from Queue") { model.queue.remove(job) }
                            }
                            Button("Show Video in Finder") { model.reveal(job.video) }
                        }
                }
            }
            .listStyle(.sidebar)
            .accessibilityLabel("Queue")
            Divider()
            HStack {
                Button { model.chooseVideos() } label: { Label("Add…", systemImage: "plus") }
                    .accessibilityLabel("Add videos")
                Spacer()
                Button("Clear Finished") { model.queue.removeFinished() }
                    .disabled(!model.queue.jobs.contains { $0.state.isFinished })
            }
            .buttonStyle(.borderless)
            .padding(8)
        }
    }
}

struct JobRow: View {
    let job: Job
    let canStart: Bool

    var body: some View {
        HStack(spacing: 8) {
            StateIcon(state: job.state)
            VStack(alignment: .leading, spacing: 2) {
                Text(job.name).lineLimit(1).truncationMode(.middle)
                HStack(spacing: 6) {
                    Text(stateText)
                    if let d = job.duration { Text("· \(Clock.format(d))") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var stateText: String {
        switch job.state {
        case .waiting: return canStart ? "Waiting" : "Waiting for the checks"
        case .running:
            if job.stopRequested { return "Stopping…" }
            if let s = job.progress.runningStage, let f = s.fraction { return "\(s.name) \(Int(f * 100))%" }
            return job.progress.runningStage?.name ?? "Starting…"
        case .done: return "Done"
        case .failed: return "Failed"
        case .cancelled: return "Stopped"
        }
    }
}

struct StateIcon: View {
    let state: JobState

    var body: some View {
        Group {
            switch state {
            case .waiting: Image(systemName: "clock").foregroundStyle(.secondary)
            case .running: ProgressView().controlSize(.small)
            case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .failed: Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
            case .cancelled: Image(systemName: "stop.circle").foregroundStyle(.secondary)
            }
        }
        .frame(width: 18, height: 18)
        .accessibilityLabel(state.rawValue)
    }
}
