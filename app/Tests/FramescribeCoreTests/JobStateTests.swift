import Foundation
import Testing
@testable import FramescribeCore

/// Lines of an event stream, written the way the CLI writes them.
enum Lines {
    static func runStart(_ stages: [String] = ["ingest", "asr.parakeet", "asr.whisperkit", "asr", "visual", "ocr", "report"],
                         duration: String = "180.0") -> String {
        let list = stages.map { name in
            name == "asr.whisperkit" ? #"{"name": "asr.whisperkit", "skipped_reason": "no CLI configured"}"#
                : #"{"name": "\#(name)", "skipped_reason": null}"#
        }.joined(separator: ", ")
        return #"{"event": "run_start", "t": 0.1, "source": "/v/talk.mp4", "duration": \#(duration), "run_dir": "/o/talk", "stages": [\#(list)]}"#
    }

    static func start(_ s: String, t: Double = 1) -> String { #"{"event": "stage_start", "t": \#(t), "stage": "\#(s)"}"# }

    static func progress(_ s: String, _ done: Double, _ total: Double, unit: String = "chunks", note: String? = nil) -> String {
        let n = note.map { "\"\($0)\"" } ?? "null"
        return #"{"event": "stage_progress", "t": 2, "stage": "\#(s)", "done": \#(done), "total": \#(total), "unit": "\#(unit)", "note": \#(n)}"#
    }

    static func end(_ s: String, _ status: String, summary: String = "", error: String? = nil, log: String? = nil,
                    kind: String? = nil) -> String {
        var extra = #""summary": "\#(summary)""#
        if let error { extra = #""error": "\#(error)", "log_path": \#(log.map { "\"\($0)\"" } ?? "null")"# }
        if let kind { extra += #", "error_kind": "\#(kind)""# }
        return #"{"event": "stage_end", "t": 3, "stage": "\#(s)", "status": "\#(status)", "wall_seconds": 2.5, \#(extra)}"#
    }

    static func skipped() -> String {
        #"{"event": "stage_end", "t": 0.2, "stage": "asr.whisperkit", "status": "skipped", "wall_seconds": 0.0, "reason": "no CLI configured"}"#
    }

    static func log(_ s: String, _ m: String) -> String { #"{"event": "log", "t": 2.5, "stage": "\#(s)", "message": "\#(m)"}"# }

    static func runEnd(_ status: String, error: String? = nil) -> String {
        let err = error.map { #", "error": "\#($0)""# } ?? ""
        return #"{"event": "run_end", "t": 9, "status": "\#(status)", "run_dir": "/o/talk", "wall_seconds": 9.0, "outputs": ["/o/talk/report.md"], "completeness": {"summary": "filled 2 words", "filled_share": 0.01, "filled_words": 2, "warning": null}, "warnings": ["a warning"]\#(err)}"#
    }

    static let everyStage = ["ingest", "asr.parakeet", "asr", "visual", "ocr", "report"]
}

@MainActor
@Suite("Job state machine")
struct JobStateTests {
    let t0 = Date(timeIntervalSince1970: 1_000_000)

    func started() -> Job {
        let job = Job(video: URL(fileURLWithPath: "/v/talk.mp4"))
        job.markStarted(runDirectory: URL(fileURLWithPath: "/o/talk"), appLog: URL(fileURLWithPath: "/o/talk/framescribe-app.log"), at: t0)
        return job
    }

    @Test func aFreshRunThroughProgressToDone() {
        let job = started()
        #expect(job.state == .running)
        job.receive(stdoutLine: Lines.runStart(), at: t0)
        job.receive(stdoutLine: Lines.skipped(), at: t0)
        #expect(job.progress.stages.map(\.name) == ["ingest", "asr.parakeet", "asr.whisperkit", "asr", "visual", "ocr", "report"])
        #expect(job.progress.stages.map(\.status) == [.pending, .pending, .skipped, .pending, .pending, .pending, .pending])
        #expect(job.duration == 180)

        job.receive(stdoutLine: Lines.start("ingest"), at: t0)
        job.receive(stdoutLine: Lines.progress("ingest", 90, 180, unit: "audio_s"), at: t0.addingTimeInterval(10))
        let ingest = job.progress.runningStage!
        #expect(ingest.name == "ingest" && ingest.fraction == 0.5)
        #expect(ingest.eta(now: t0.addingTimeInterval(10)) == 10)  // 10 s for half: 10 s left
        #expect(ingest.amountText == "1:30 / 3:00 of audio")
        #expect(job.progress.overallLine(now: t0.addingTimeInterval(10)) == "Stage 1 of 7: ingest · elapsed 0:10 · video 3:00")
        job.receive(stdoutLine: Lines.end("ingest", "done", summary: "3:00 of audio"), at: t0.addingTimeInterval(20))

        job.receive(stdoutLine: Lines.start("asr.parakeet"), at: t0.addingTimeInterval(20))
        job.receive(stdoutLine: Lines.progress("asr.parakeet", 14, 57, note: "pass 1, chunk 14/57"), at: t0.addingTimeInterval(34))
        let asr = job.progress.runningStage!
        #expect(asr.note == "pass 1, chunk 14/57" && asr.amountText == "14 / 57 chunks")
        #expect(asr.elapsed(now: t0.addingTimeInterval(34)) == 14)
        job.receive(stdoutLine: Lines.log("asr.parakeet", "loading the model"), at: t0.addingTimeInterval(35))
        #expect(job.progress.logLines.map(\.message) == ["loading the model"])
        #expect(job.progress.silence(now: t0.addingTimeInterval(65)) == 30)  // the window says "no update for 0:30"

        for s in ["asr.parakeet", "asr", "visual", "ocr", "report"] {
            if s != "asr.parakeet" { job.receive(stdoutLine: Lines.start(s), at: t0.addingTimeInterval(40)) }
            job.receive(stdoutLine: Lines.end(s, "done", summary: "\(s) ok"), at: t0.addingTimeInterval(50))
        }
        job.receive(stdoutLine: Lines.runEnd("ok"), at: t0.addingTimeInterval(60))
        job.finish(exit: .exited(0), at: t0.addingTimeInterval(60))
        #expect(job.state == .done && job.failure == nil)
        #expect(job.report?.path == "/o/talk/report.md")
        #expect(job.progress.completeness?.summary == "filled 2 words" && job.progress.warnings == ["a warning"])
        #expect(job.progress.finishedStageCount == 7 && job.progress.silence(now: t0.addingTimeInterval(99)) == nil)
        #expect(job.progress.elapsed(now: t0.addingTimeInterval(99)) == 9)  // run_end's wall time once ended
    }

    @Test func aCachedRunFinishesEveryStageAsCached() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        for s in Lines.everyStage {
            job.receive(stdoutLine: Lines.start(s), at: t0)
            job.receive(stdoutLine: Lines.end(s, "cached"), at: t0)
        }
        job.receive(stdoutLine: Lines.runEnd("ok"), at: t0)
        job.finish(exit: .exited(0), at: t0)
        #expect(job.state == .done)
        #expect(job.progress.stages.allSatisfy { $0.status == .cached })
    }

    @Test func aFailedStageGivesTheStageTheMessageAndItsLog() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: Lines.start("ingest"), at: t0)
        job.receive(stdoutLine: Lines.end("ingest", "done"), at: t0)
        job.receive(stdoutLine: Lines.start("asr"), at: t0)
        job.receive(stdoutLine: Lines.end("asr", "failed", error: "36 of the words were filled in", log: "/o/talk/work/asr.log"), at: t0)
        job.receive(stdoutLine: Lines.runEnd("failed", error: "36 of the words were filled in"), at: t0)
        job.receive(stderrLine: "error: 36 of the words were filled in")
        job.finish(exit: .exited(2), at: t0)
        #expect(job.state == .failed)
        #expect(job.failure == JobFailure(stage: "asr", message: "36 of the words were filled in", logPath: "/o/talk/work/asr.log"))
        #expect(job.report == nil)
    }

    @Test func aFailureBeforeAnyEventUsesTheErrorLineAndTheAppLog() {
        let job = started()
        job.receive(stderrLine: "error: speech model: missing. Fix: framescribe setup.")
        job.finish(exit: .exited(2), at: t0)
        #expect(job.failure == JobFailure(stage: nil, message: "speech model: missing. Fix: framescribe setup.",
                                          logPath: "/o/talk/framescribe-app.log"))
    }

    @Test func aVideoWithoutAudioGetsAPlainMessageFromTheErrorKindNotTheWording() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: Lines.start("asr.parakeet"), at: t0)
        job.receive(stdoutLine: Lines.end("asr.parakeet", "failed", error: "worded in any way", kind: "no_audio"), at: t0)
        job.receive(stdoutLine: #"{"event": "run_end", "t": 9, "status": "failed", "run_dir": "/o/talk", "wall_seconds": 9.0, "error": "worded in any way", "error_kind": "no_audio", "completeness": null, "warnings": []}"#, at: t0)
        job.finish(exit: .exited(2), at: t0)
        #expect(job.state == .failed && job.failure?.kind == .noAudio)
        #expect(job.failure?.message == "This video has no audio track, so there is nothing to transcribe.")
        #expect(job.failure?.message.contains("--") == false && job.failure?.detail == "worded in any way")
        // the same wording without the kind is an ordinary failure: the text is never matched
        let plain = started()
        plain.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        plain.receive(stdoutLine: Lines.end("asr.parakeet", "failed", error: "/v/talk.mp4 has no audio stream, so there is nothing to transcribe; use --transcript"), at: t0)
        plain.finish(exit: .exited(2), at: t0)
        #expect(plain.failure?.kind == .general && plain.failure?.message.hasSuffix("use --transcript") == true)
    }

    @Test func aRefusedTranscriptIsExplainedWithoutACommandLineFlag() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: Lines.end("asr", "failed", error: "pass --allow-incomplete", kind: "incomplete_transcript"), at: t0)
        job.receive(stdoutLine: #"{"event": "run_end", "t": 9, "status": "failed", "run_dir": "/o/talk", "wall_seconds": 9.0, "error": "x", "error_kind": "incomplete_transcript", "completeness": {"summary": "s", "filled_share": 0.0762, "filled_words": 128, "primary_words": 1552, "limit": 0.03, "above_limit": false, "warning": null}, "warnings": []}"#, at: t0)
        job.finish(exit: .exited(2), at: t0)
        #expect(job.failure?.kind == .incompleteTranscript)
        #expect(job.failure?.message.contains("128 words (7.6%)") == true && job.failure?.message.contains("--") == false)
    }

    @Test func aDoneJobAboveTheCompletenessLimitShowsTheWarningWithTheShare() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: #"{"event": "run_end", "t": 9, "status": "ok", "run_dir": "/o/talk", "wall_seconds": 9.0, "outputs": ["/o/talk/report.md"], "completeness": {"summary": "s", "filled_share": 0.07619, "filled_words": 128, "primary_words": 1552, "limit": 0.03, "above_limit": true, "warning": "128 of the transcript's 1680 words (7.6%) were filled in"}, "warnings": ["128 of the transcript's 1680 words (7.6%) were filled in"]}"#, at: t0)
        #expect(job.completenessNotice == nil)  // not before it is done
        job.finish(exit: .exited(0), at: t0)
        #expect(job.state == .done && job.report?.path == "/o/talk/report.md")
        let notice = job.completenessNotice
        #expect(notice?.hasPrefix("128 of the transcript's 1680 words (7.6%) had to be filled in from other sources, more than the usual 3.0%") == true)
        #expect(notice?.contains("check them against the recording") == true)
        // within the limit: no notice
        let quiet = started()
        quiet.receive(stdoutLine: Lines.runEnd("ok"), at: t0)
        quiet.finish(exit: .exited(0), at: t0)
        #expect(quiet.state == .done && quiet.completenessNotice == nil && quiet.progress.completeness?.aboveLimit == false)
    }

    @Test func aChildKilledBySignalAfterStopIsStoppedAndWithoutStopAFailureNamingTheSignal() {
        // the MLX extension once aborted (SIGABRT) while the model loaded, after Stop: no run_end, some stderr
        let stopped = started()
        stopped.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        stopped.receive(stdoutLine: Lines.start("asr.parakeet"), at: t0)
        stopped.requestStop()
        stopped.receive(stderrLine: "libc++abi: terminating")
        stopped.finish(exit: .signaled(SIGABRT), at: t0)
        #expect(stopped.state == .cancelled && stopped.failure == nil)
        let crashed = started()
        crashed.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        crashed.receive(stdoutLine: Lines.start("asr.parakeet"), at: t0)
        crashed.receive(stderrLine: "libc++abi: terminating")
        crashed.finish(exit: .signaled(SIGABRT), at: t0)
        #expect(crashed.state == .failed && crashed.failure?.kind == .crashed(signal: SIGABRT))
        #expect(crashed.failure?.message.contains("signal 6") == true && crashed.failure?.detail == "libc++abi: terminating")
    }

    @Test func closingTheWindowWhileWorkRunsKeepsTheAppAndOnlyQuitAsks() {
        #expect(WindowPolicy.quitsWhenLastWindowCloses(hasRunningChild: false))
        #expect(!WindowPolicy.quitsWhenLastWindowCloses(hasRunningChild: true))
        #expect(WindowPolicy.reopensWindow(hasVisibleWindows: false) && !WindowPolicy.reopensWindow(hasVisibleWindows: true))
        #expect(WindowPolicy.onQuit(hasRunningChild: true) == .ask && WindowPolicy.onQuit(hasRunningChild: false) == .now)
    }

    @Test func aCrashWithoutAnyOutputSaysHowItStopped() {
        let job = started()
        job.finish(exit: .signaled(9), at: t0)
        #expect(job.state == .failed)
        #expect(job.failure?.message.contains("signal 9") == true)
    }

    @Test func exitZeroWithoutRunEndIsNotDone() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.finish(exit: .exited(0), at: t0)
        #expect(job.state == .failed)
    }

    @Test func aStoppedRunIsCancelledNotFailed() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: Lines.start("ingest"), at: t0)
        job.requestStop()
        job.receive(stdoutLine: Lines.end("ingest", "failed", error: "interrupted"), at: t0)
        job.receive(stdoutLine: Lines.runEnd("interrupted", error: "interrupted"), at: t0)
        job.finish(exit: .exited(130), at: t0)
        #expect(job.state == .cancelled && job.failure == nil)
    }

    @Test func anInterruptTheAppDidNotAskForIsAFailure() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: Lines.runEnd("interrupted", error: "terminated"), at: t0)
        job.finish(exit: .exited(143), at: t0)
        #expect(job.state == .failed && job.failure?.message == "terminated")
    }

    @Test func aRunThatEndedJustBeforeTheStopIsDone() {
        let job = started()
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        job.receive(stdoutLine: Lines.runEnd("ok"), at: t0)
        job.requestStop()
        job.finish(exit: .exited(0), at: t0)
        #expect(job.state == .done)
    }

    @Test func malformedStdoutLinesAreCountedAndOtherwiseIgnored() {
        let job = started()
        job.receive(stdoutLine: "not json", at: t0)
        job.receive(stdoutLine: "", at: t0)
        job.receive(stdoutLine: Lines.runStart(Lines.everyStage), at: t0)
        #expect(job.progress.malformedLines == 1 && job.progress.stages.count == 6)
    }

    @Test func eventsOfAStageRunStartDidNotListStillShow() {
        var p = RunProgress()
        p.apply(.stageStart(t: 0, stage: "extra"), at: t0)
        #expect(p.stages.map(\.name) == ["extra"] && p.runningStage?.name == "extra")
    }

    @Test func theLogKeepsTheLastLinesAndCountsTheDroppedOnes() {
        var p = RunProgress()
        for i in 0..<(RunProgress.maxLogLines + 5) {
            p.apply(.log(LogEvent(t: 0, stage: "s", message: "m\(i)")), at: t0)
        }
        #expect(p.logLines.count == RunProgress.maxLogLines && p.droppedLogLines == 5)
        #expect(p.logLines.first?.message == "m5" && Set(p.logLines.map(\.id)).count == p.logLines.count)
    }

    @Test func etaNeedsProgressAndAStageThatRuns() {
        var s = StageView(name: "ocr", status: .running)
        s.startedAt = t0
        s.total = 10
        #expect(s.eta(now: t0.addingTimeInterval(5)) == nil)  // nothing done yet
        s.done = 10
        #expect(s.eta(now: t0.addingTimeInterval(5)) == nil)  // complete
        s.done = 4
        #expect(s.eta(now: t0.addingTimeInterval(8)) == 12)
        s.status = .done
        #expect(s.eta(now: t0.addingTimeInterval(8)) == nil)
    }

    @Test func clockFormats() {
        #expect(Clock.format(0) == "0:00")
        #expect(Clock.format(83.9) == "1:23")
        #expect(Clock.format(3723) == "1:02:03")
        #expect(Clock.format(-5) == "0:00")
        #expect(Clock.number(14) == "14" && Clock.number(2.5) == "2.5")
    }
}
