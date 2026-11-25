import Foundation
import Testing
@testable import FramescribeCore

/// Event streams recorded from the embedded runtime on a 3-minute excerpt (paths rewritten to neutral ones):
/// a fresh run, the same run again (cached), one stopped during the visual stage, one stopped while MLX was being
/// imported (Python aborted: no run_end), one refused by the completeness gate, and `setup` with the model present.
@MainActor
@Suite("Recorded event streams")
struct RecordedStreamTests {
    let t0 = Date(timeIntervalSince1970: 2_000_000)

    func play(_ fixture: String, exit: ProcessExit, stop: Bool = false) -> Job {
        let job = Job(video: URL(fileURLWithPath: "/home/user/Movies/excerpt.mp4"))
        job.markStarted(runDirectory: URL(fileURLWithPath: "/home/user/Documents/framescribe/excerpt"),
                        appLog: URL(fileURLWithPath: "/home/user/Documents/framescribe/excerpt/framescribe-app.log"), at: t0)
        let lines = Fixture.lines(fixture)
        for (i, line) in lines.enumerated() {
            if stop && i == lines.count / 2 { job.requestStop() }
            job.receive(stdoutLine: line, at: t0.addingTimeInterval(Double(i)))
        }
        job.finish(exit: exit, at: t0.addingTimeInterval(Double(lines.count)))
        return job
    }

    @Test func everyRecordedLineDecodes() {
        for name in ["run_fresh.jsonl", "run_cached.jsonl", "run_stopped_in_visual.jsonl", "run_aborted_during_import.jsonl",
                     "run_gate_failed.jsonl", "setup_present.jsonl"] {
            for line in Fixture.lines(name) {
                guard case let .success(event) = EventDecoder.decode(line) else {
                    Issue.record("\(name): \(line.prefix(80))")
                    continue
                }
                if case .unknown = event { Issue.record("\(name): unknown event \(line.prefix(60))") }
            }
        }
    }

    @Test func theFreshRunHasEveryEventKind() {
        let events = Fixture.events("run_fresh.jsonl")
        func count(_ test: (ToolEvent) -> Bool) -> Int { events.filter(test).count }
        #expect(count { if case .runStart = $0 { return true }; return false } == 1)
        #expect(count { if case .stageStart = $0 { return true }; return false } == 6)
        #expect(count { if case .stageProgress = $0 { return true }; return false } > 50)
        #expect(count { if case .log = $0 { return true }; return false } >= 1)
        #expect(count { if case .stageEnd = $0 { return true }; return false } == 7)
        #expect(count { if case .runEnd = $0 { return true }; return false } == 1)
        guard case let .runStart(start) = events.first else { Issue.record("no run_start first"); return }
        #expect(start.stages.map(\.name) == ["ingest", "asr.parakeet", "asr.whisperkit", "asr", "visual", "ocr", "report"])
        #expect(start.stages[2].skippedReason?.hasPrefix("no CLI configured") == true)
    }

    @Test func aFreshRunEndsDoneWithTheCompletenessLineAndTheWarnings() {
        let job = play("run_fresh.jsonl", exit: .exited(0))
        #expect(job.state == .done)
        let p = job.progress
        #expect(p.stages.map(\.status) == [.done, .done, .skipped, .done, .done, .done, .done])
        #expect(abs((job.duration ?? 0) - 180.0) < 0.1)
        #expect(p.completeness?.summary.hasPrefix("cross-checked against the second parakeet pass only") == true)
        #expect(p.completeness?.filledWords == 3)
        #expect(p.warnings.count == 2 && p.warnings.allSatisfy { $0.contains("without punctuation") })
        #expect(p.outputs.count == 5 && p.outputs.first?.hasSuffix("/excerpt/report.md") == true)
        #expect(job.report?.path == "/home/user/Documents/framescribe/excerpt/report.md")
        #expect(p.stages.first { $0.name == "asr.parakeet" }?.summary?.contains("realtime") == true)
        #expect(p.malformedLines == 0)
    }

    @Test func progressDuringTheFreshRunIsDeterminate() {
        let job = Job(video: URL(fileURLWithPath: "/home/user/Movies/excerpt.mp4"))
        job.markStarted(runDirectory: URL(fileURLWithPath: "/o"), appLog: nil, at: t0)
        var sawParakeetNote = false
        var fractions: [Double] = []
        for (i, line) in Fixture.lines("run_fresh.jsonl").enumerated() {
            job.receive(stdoutLine: line, at: t0.addingTimeInterval(Double(i)))
            if let s = job.progress.runningStage, s.name == "asr.parakeet", let f = s.fraction {
                fractions.append(f)
                if s.note?.contains("chunk") == true { sawParakeetNote = true }
            }
        }
        #expect(sawParakeetNote)
        #expect(!fractions.isEmpty && fractions.allSatisfy { $0 >= 0 && $0 <= 1 })
    }

    @Test func aCachedRunHasNoProgressAndEveryStageCached() {
        let events = Fixture.events("run_cached.jsonl")
        #expect(!events.contains { if case .stageProgress = $0 { return true }; return false })
        let job = play("run_cached.jsonl", exit: .exited(0))
        #expect(job.state == .done)
        #expect(job.progress.stages.map(\.status) == [.cached, .cached, .skipped, .cached, .cached, .cached, .cached])
    }

    @Test func theRunStoppedInVisualIsCancelledAndKeepsTheFinishedStages() {
        let job = play("run_stopped_in_visual.jsonl", exit: .exited(130), stop: true)
        #expect(job.state == .cancelled)
        let p = job.progress
        #expect(p.endStatus == "interrupted" && p.endError == "interrupted")
        #expect(p.stages.map(\.status) == [.done, .done, .skipped, .done, .failed, .pending, .pending])
        #expect(p.failedStage?.name == "visual" && p.failedStage?.reason == "interrupted")
        // without the app asking for it, the same stream is a failure
        let unasked = play("run_stopped_in_visual.jsonl", exit: .exited(130))
        #expect(unasked.state == .failed && unasked.failure?.stage == "visual")
    }

    @Test func aRunWhosePythonAbortedHasNoRunEndAndStillEnds() {
        let stopped = play("run_aborted_during_import.jsonl", exit: .signaled(6), stop: true)
        #expect(stopped.state == .cancelled)
        #expect(stopped.progress.endStatus == nil && stopped.progress.runningStage?.name == "asr.parakeet")
        let crashed = play("run_aborted_during_import.jsonl", exit: .signaled(6))
        #expect(crashed.state == .failed && crashed.failure?.message.contains("signal 6") == true)
        #expect(crashed.failure?.logPath == "/home/user/Documents/framescribe/excerpt/framescribe-app.log")
    }

    @Test func aRefusedTranscriptFailsInTheAsrStageWithItsLogAndFigures() {
        let job = play("run_gate_failed.jsonl", exit: .exited(2))
        #expect(job.state == .failed)
        let f = job.failure
        #expect(f?.stage == "asr")
        #expect(f?.message.contains("had to be filled in from other sources") == true)
        // the asr stage wrote no log before the gate refused it, so "Show log" reveals the app's log of the job
        #expect(f?.logPath == "/home/user/Documents/framescribe/excerpt/framescribe-app.log")
        #expect(job.progress.completeness?.filledWords == 3)  // the refused transcript's figures
        #expect(job.progress.stages.prefix(2).allSatisfy { $0.status == .cached })
    }

    @Test func setupWithTheModelPresentEndsAtOnce() {
        var p = SetupProgress()
        Fixture.events("setup_present.jsonl").forEach { p.apply($0) }
        #expect(p.phase == .finished)
    }
}
