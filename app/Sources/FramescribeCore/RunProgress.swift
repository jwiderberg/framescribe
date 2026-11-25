import Foundation

/// One stage of a run as the window shows it.
public struct StageView: Sendable, Equatable, Identifiable {
    public var id: String { name }
    public let name: String
    public var status: StageStatus = .pending
    public var done: Double = 0
    public var total: Double = 0
    public var unit: String = ""
    public var note: String?
    /// When the app saw `stage_start` (wall clock of this machine).
    public var startedAt: Date?
    public var wallSeconds: Double?
    public var summary: String?
    /// Why it was skipped, or why it failed.
    public var reason: String?
    public var logPath: String?
    /// Why it failed, machine-readable, when the tool says.
    public var errorKind: ToolErrorKind?

    public init(name: String, status: StageStatus = .pending, reason: String? = nil) {
        self.name = name
        self.status = status
        self.reason = reason
    }

    /// 0...1 when the stage has reported a total.
    public var fraction: Double? {
        guard total > 0 else { return nil }
        return min(1, max(0, done / total))
    }

    public func elapsed(now: Date) -> Double? {
        if let wall = wallSeconds, status != .running { return wall }
        return startedAt.map { max(0, now.timeIntervalSince($0)) }
    }

    /// Seconds left, extrapolated from the progress so far; nil until there is some.
    public func eta(now: Date) -> Double? {
        guard status == .running, let elapsed = elapsed(now: now), elapsed > 0, done > 0, total > done else { return nil }
        return elapsed * (total - done) / done
    }

    /// "1:23 / 3:00" for seconds of audio, "14 / 57" otherwise.
    public var amountText: String? {
        guard total > 0 else { return nil }
        if unit == "audio_s" { return "\(Clock.format(done)) / \(Clock.format(total)) of audio" }
        let unitText = unit.isEmpty ? "" : " \(unit)"
        return "\(Clock.number(done)) / \(Clock.number(total))\(unitText)"
    }
}

public struct LogLine: Sendable, Equatable, Identifiable {
    public let id: Int
    public let t: Double
    public let stage: String
    public let message: String
}

/// Everything the event stream of one run has said so far. Pure state: `apply` takes the time an event arrived.
public struct RunProgress: Sendable, Equatable {
    public static let maxLogLines = 2000

    public var source: String?
    public var duration: Double?
    public var runDir: String?
    public var stages: [StageView] = []
    public var startedAt: Date?
    public var lastEventAt: Date?
    public var endStatus: String?
    public var endError: String?
    public var endErrorKind: ToolErrorKind?
    public var outputs: [String] = []
    public var completeness: CompletenessFigures?
    public var warnings: [String] = []
    public var logLines: [LogLine] = []
    public var droppedLogLines = 0
    public var wallSeconds: Double?
    /// Lines on stdout that were not events (should never happen; counted so the window can say so).
    public var malformedLines = 0

    public init() {}

    public var hasStarted: Bool { startedAt != nil }
    public var hasEnded: Bool { endStatus != nil }
    public var runningStage: StageView? { stages.first { $0.status == .running } }
    public var failedStage: StageView? { stages.first { $0.status == .failed } }
    public var finishedStageCount: Int { stages.filter { $0.status.isFinished }.count }

    public mutating func apply(_ event: ToolEvent, at now: Date) {
        lastEventAt = now
        switch event {
        case let .runStart(e):
            startedAt = now
            source = e.source
            duration = e.duration
            runDir = e.runDir
            stages = e.stages.map { StageView(name: $0.name, reason: $0.skippedReason) }
        case let .stageStart(_, stage):
            update(stage) {
                $0.status = .running
                $0.startedAt = now
                $0.done = 0
                $0.total = 0
                $0.note = nil
            }
        case let .stageProgress(e):
            update(e.stage) {
                $0.done = e.done
                $0.total = e.total
                $0.unit = e.unit
                $0.note = e.note
                if $0.status == .pending { $0.status = .running; $0.startedAt = now }
            }
        case let .log(e):
            appendLog(LogLine(id: logLines.count + droppedLogLines, t: e.t, stage: e.stage, message: e.message))
        case let .stageEnd(e):
            update(e.stage) {
                $0.status = e.status
                $0.wallSeconds = e.wallSeconds
                $0.summary = e.summary
                $0.reason = e.reason ?? e.error
                $0.logPath = e.logPath
                $0.errorKind = e.errorKind
                if e.status == .done || e.status == .cached, $0.total > 0 { $0.done = $0.total }
            }
        case let .runEnd(e):
            endStatus = e.status
            endError = e.error
            endErrorKind = e.errorKind
            outputs = e.outputs
            completeness = e.completeness
            warnings = e.warnings
            wallSeconds = e.wallSeconds
            if !e.runDir.isEmpty { runDir = e.runDir }
        case .setupStart, .setupProgress, .setupEnd, .unknown:
            break
        }
    }

    public mutating func noteMalformedLine() { malformedLines += 1 }

    private mutating func appendLog(_ line: LogLine) {
        logLines.append(line)
        if logLines.count > Self.maxLogLines {
            let excess = logLines.count - Self.maxLogLines
            logLines.removeFirst(excess)
            droppedLogLines += excess
        }
    }

    private mutating func update(_ name: String, _ change: (inout StageView) -> Void) {
        if let i = stages.firstIndex(where: { $0.name == name }) {
            change(&stages[i])
        } else {  // a stage run_start did not list: show it rather than drop its events
            var s = StageView(name: name)
            change(&s)
            stages.append(s)
        }
    }

    public func elapsed(now: Date) -> Double? {
        if let wall = wallSeconds { return wall }
        return startedAt.map { max(0, now.timeIntervalSince($0)) }
    }

    /// Seconds since the last event, for "still working" when the stream is quiet.
    public func silence(now: Date) -> Double? {
        guard !hasEnded, let last = lastEventAt else { return nil }
        return max(0, now.timeIntervalSince(last))
    }

    /// "Stage 3 of 7 · asr.parakeet · elapsed 0:01:23 · video 0:03:00"
    public func overallLine(now: Date) -> String {
        var parts: [String] = []
        if !stages.isEmpty {
            let current = runningStage.map { s in (stages.firstIndex(of: s) ?? 0) + 1 }
            if let index = current, let s = runningStage {
                parts.append("Stage \(index) of \(stages.count): \(s.name)")
            } else {
                parts.append("\(finishedStageCount) of \(stages.count) stages finished")
            }
        }
        if let elapsed = elapsed(now: now) { parts.append("elapsed \(Clock.format(elapsed))") }
        if let d = duration { parts.append("video \(Clock.format(d))") }
        return parts.joined(separator: " · ")
    }
}

public enum Clock {
    /// 83.4 -> "1:23", 3723 -> "1:02:03".
    public static func format(_ seconds: Double) -> String {
        let s = max(0, Checked.int(seconds.rounded(.down)) ?? 0)
        let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }

    public static func number(_ value: Double) -> String {
        value == value.rounded() && abs(value) < 1e15 ? String(Checked.int(value) ?? 0) : String(format: "%.1f", value)
    }
}
