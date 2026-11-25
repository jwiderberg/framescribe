import Foundation
import Observation

public enum JobState: String, Sendable, Equatable {
    case waiting, running, done, failed, cancelled

    public var isFinished: Bool { self == .done || self == .failed || self == .cancelled }
}

public struct JobFailure: Sendable, Equatable {
    public enum Kind: Sendable, Equatable {
        /// Any failure the app has no explanation of its own for: the tool's message is shown.
        case general
        /// The video has no audio track (from the event stream's `error_kind`, never from the wording).
        case noAudio
        /// The completeness gate refused the transcript (only with --strict-completeness; the app never passes it).
        case incompleteTranscript
        /// The child died by a signal the app did not send.
        case crashed(signal: Int32)
        /// The video was gone when its turn came.
        case videoMissing
    }

    /// The stage that failed, when the event stream names one.
    public let stage: String?
    /// What the window shows: plain words of the app's own for the kinds it knows, else the tool's message.
    public let message: String
    /// The stage's log file (`work/<stage>.log`) when there is one, else the app's log of the job.
    public let logPath: String?
    public let kind: Kind
    /// The tool's own message, when the window shows another one.
    public let detail: String?

    public init(stage: String?, message: String, logPath: String?, kind: Kind = .general, detail: String? = nil) {
        self.stage = stage
        self.message = message
        self.logPath = logPath
        self.kind = kind
        self.detail = detail
    }

    public static let noAudioMessage = "This video has no audio track, so there is nothing to transcribe."
}

/// One video in the queue and everything its run has said.
@MainActor @Observable
public final class Job: Identifiable {
    public static let stderrTailLines = 40

    public let id = UUID()
    public let video: URL
    public internal(set) var state: JobState = .waiting
    /// Known from the file before the run (when the system can read it), else from `run_start`.
    public var duration: Double?
    public internal(set) var progress = RunProgress()
    public internal(set) var stopRequested = false
    public internal(set) var exitStatus: ProcessExit?
    public internal(set) var failure: JobFailure?
    public internal(set) var runDirectory: URL?
    public internal(set) var appLog: URL?
    public internal(set) var stderrTail: [String] = []
    public internal(set) var startedAt: Date?
    public internal(set) var finishedAt: Date?
    /// Another active job in the queue has a video of the same file name (from another folder); each gets its own
    /// output folder (see OutputLocation.chooseRunDirectory).
    public internal(set) var sharesOutputWith: String?

    public init(video: URL) { self.video = video.standardizedFileURL }

    public var name: String { video.lastPathComponent }

    public var report: URL? {
        guard state == .done, let dir = runDirectory else { return nil }
        return OutputLocation.report(in: dir)
    }

    /// A done job whose transcript had more filled in than usual: the warning line it shows above its buttons.
    public var completenessNotice: String? {
        guard state == .done else { return nil }
        return progress.completeness?.aboveLimitNotice
    }

    // ---- driven by the queue (and by the tests) ----------------------------------------------------------------

    func markStarted(runDirectory: URL, appLog: URL?, at now: Date) {
        state = .running
        self.runDirectory = runDirectory
        self.appLog = appLog
        startedAt = now
    }

    /// One line of the child's stdout: an event, or (never expected) something else, which is counted.
    func receive(stdoutLine line: String, at now: Date) {
        switch EventDecoder.decode(line) {
        case let .success(event):
            progress.apply(event, at: now)
            if case let .runStart(e) = event {
                if let d = e.duration { duration = d }
                if !e.runDir.isEmpty { runDirectory = URL(fileURLWithPath: e.runDir, isDirectory: true) }
            }
        case .failure(.empty):
            break
        case .failure:
            progress.noteMalformedLine()
        }
    }

    func receive(stderrLine line: String) {
        stderrTail.append(line)
        if stderrTail.count > Self.stderrTailLines { stderrTail.removeFirst(stderrTail.count - Self.stderrTailLines) }
    }

    func requestStop() { stopRequested = true }

    /// The end of the run: done only when the process exited 0 and the stream ended with `run_end` ok.
    func finish(exit: ProcessExit, at now: Date) {
        exitStatus = exit
        finishedAt = now
        if exit.isSuccess && progress.endStatus == "ok" {
            state = .done
            return
        }
        if stopRequested {
            state = .cancelled
            return
        }
        state = .failed
        failure = Self.failure(progress: progress, stderrTail: stderrTail, exit: exit, appLog: appLog)
    }

    func failToStart(_ message: String, kind: JobFailure.Kind = .general, at now: Date) {
        state = .failed
        finishedAt = now
        failure = JobFailure(stage: nil, message: message, logPath: appLog?.path, kind: kind)
    }

    static func failure(progress: RunProgress, stderrTail: [String], exit: ProcessExit, appLog: URL?) -> JobFailure {
        let stage = progress.failedStage
        let stderrError = stderrTail.last { $0.hasPrefix("error: ") }.map { String($0.dropFirst("error: ".count)) }
        let toolMessage = stage?.reason ?? progress.endError ?? stderrError
        let log = stage?.logPath ?? appLog?.path
        switch stage?.errorKind ?? progress.endErrorKind {
        case .noAudio?:
            return JobFailure(stage: stage?.name, message: JobFailure.noAudioMessage, logPath: log, kind: .noAudio,
                              detail: toolMessage)
        case .incompleteTranscript?:
            return JobFailure(stage: stage?.name, message: incompleteMessage(progress.completeness), logPath: log,
                              kind: .incompleteTranscript, detail: toolMessage)
        case .other?, nil:
            break
        }
        if toolMessage == nil, case let .signaled(signal) = exit {
            // a crash: no message of the tool's own; name the signal, the last stderr line is the detail
            return JobFailure(stage: stage?.name, message: "framescribe stopped unexpectedly (\(exit)).", logPath: log,
                              kind: .crashed(signal: signal), detail: stderrTail.last)
        }
        let message = toolMessage ?? stderrTail.last
            ?? (exit.isSuccess ? "the run ended without reporting its result" : "framescribe stopped (\(exit))")
        return JobFailure(stage: stage?.name, message: message, logPath: log)
    }

    static func incompleteMessage(_ c: CompletenessFigures?) -> String {
        var amount = "Too much of the transcript"
        if let c, let filled = c.filledWords, let share = c.filledShare {
            amount = "\(filled) words (\(CompletenessFigures.percent(share)))"
        }
        return "\(amount) had to be filled in from other sources: the speech recognition missed part of the speech, "
            + "so the transcript was not kept."
    }

    func noteSharedOutput(with other: String?) { sharesOutputWith = other }
}
