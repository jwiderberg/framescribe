import Foundation
import Testing
@testable import FramescribeCore

@Suite("Event decoding")
struct EventDecodingTests {
    func decode(_ line: String) -> ToolEvent? {
        if case let .success(e) = EventDecoder.decode(line) { return e }
        return nil
    }

    func error(_ line: String) -> EventDecodeError? {
        if case let .failure(e) = EventDecoder.decode(line) { return e }
        return nil
    }

    @Test func runStartWithStagesAndASkippedReason() {
        let e = decode(#"{"event": "run_start", "t": 0.0, "source": "/v/talk.mp4", "duration": 180.5, "run_dir": "out/talk", "stages": [{"name": "ingest", "skipped_reason": null}, {"name": "asr.whisperkit", "skipped_reason": "no CLI configured"}]}"#)
        #expect(e == .runStart(RunStartEvent(t: 0, source: "/v/talk.mp4", duration: 180.5, runDir: "out/talk",
                                             stages: [StagePlan(name: "ingest"),
                                                      StagePlan(name: "asr.whisperkit", skippedReason: "no CLI configured")])))
    }

    @Test func runStartOfARunThatFailedBeforeItStarted() {
        // the pipeline sends duration null and no stages when it fails before the probe
        let e = decode(#"{"event": "run_start", "t": 0.01, "source": "x.mp4", "duration": null, "run_dir": "o/x", "stages": []}"#)
        guard case let .runStart(s) = e else { Issue.record("not run_start"); return }
        #expect(s.duration == nil && s.stages.isEmpty)
    }

    @Test func progressWithIntegerNumbersAndANullNote() {
        let e = decode(#"{"event": "stage_progress", "t": 3, "stage": "ocr", "done": 4, "total": 17, "unit": "states", "note": null}"#)
        #expect(e == .stageProgress(StageProgressEvent(t: 3, stage: "ocr", done: 4, total: 17, unit: "states", note: nil)))
    }

    @Test func stageEndVariants() {
        #expect(decode(#"{"event": "stage_end", "t": 9.5, "stage": "ingest", "status": "cached", "wall_seconds": 0.02, "summary": "3:00"}"#)
            == .stageEnd(StageEndEvent(t: 9.5, stage: "ingest", status: .cached, wallSeconds: 0.02, summary: "3:00",
                                       reason: nil, error: nil, logPath: nil)))
        guard case let .stageEnd(failed) = decode(#"{"event": "stage_end", "t": 1, "stage": "asr", "status": "failed", "wall_seconds": 1.5, "error": "gate", "log_path": "/o/work/asr.log"}"#)
        else { Issue.record("no stage_end"); return }
        #expect(failed.status == .failed && failed.error == "gate" && failed.logPath == "/o/work/asr.log")
    }

    @Test func runEndWithCompletenessAndWarnings() {
        let e = decode(#"{"event": "run_end", "t": 80, "status": "ok", "run_dir": "o/x", "wall_seconds": 80.2, "outputs": ["o/x/report.md"], "completeness": {"primary": "parakeet", "filled_words": 3, "filled_share": 0.01, "warning": null, "summary": "cross-checked; filled 3 words"}, "warnings": ["chunk 1"]}"#)
        guard case let .runEnd(end) = e else { Issue.record("no run_end"); return }
        #expect(end.completeness == CompletenessFigures(summary: "cross-checked; filled 3 words", filledShare: 0.01, filledWords: 3, warning: nil))
        #expect(end.warnings == ["chunk 1"] && end.outputs == ["o/x/report.md"])
    }

    @Test func runEndOfAnOlderCLIWithoutTheNewFields() {
        let e = decode(#"{"event": "run_end", "t": 1, "status": "failed", "run_dir": "o", "wall_seconds": 1.0, "error": "bad"}"#)
        guard case let .runEnd(end) = e else { Issue.record("no run_end"); return }
        #expect(end.completeness == nil && end.warnings.isEmpty && end.error == "bad")
    }

    @Test func setupEvents() {
        #expect(decode(#"{"event": "setup_start", "t": 0, "repo": "r", "size_gb": 2.5, "cache_dir": "/c", "free_gb": 50.0, "files": ["config.json", "model.safetensors"]}"#)
            == .setupStart(SetupStartEvent(t: 0, repo: "r", sizeGB: 2.5, cacheDir: "/c", freeGB: 50, files: ["config.json", "model.safetensors"])))
        #expect(decode(#"{"event": "setup_progress", "t": 1, "file": "model.safetensors", "done": 1000, "total": 2508000000}"#)
            == .setupProgress(SetupProgressEvent(t: 1, file: "model.safetensors", done: 1000, total: 2_508_000_000)))
        #expect(decode(#"{"event": "setup_end", "t": 2, "status": "failed", "detail": null, "error": "no route"}"#)
            == .setupEnd(SetupEndEvent(t: 2, status: "failed", detail: nil, error: "no route")))
    }

    @Test func anUnknownEventKindIsNotAnError() {
        #expect(decode(#"{"event": "stage_heartbeat", "t": 1}"#) == .unknown(kind: "stage_heartbeat"))
    }

    @Test func malformedLines() {
        #expect(error("") == .empty)
        #expect(error("   ") == .empty)
        #expect(error("Traceback (most recent call last):") == .notJSON)
        #expect(error(#"{"event": "log", "t": 1"#) == .notJSON)  // cut off
        #expect(error("[1, 2]") == .notAnObject)
        #expect(error("42") == .notAnObject)
        #expect(error(#"{"t": 1}"#) == .missingField(event: nil, field: "event"))
        #expect(error(#"{"event": "stage_start", "t": 1}"#) == .missingField(event: "stage_start", field: "stage"))
        #expect(error(#"{"event": "stage_progress", "t": 1, "stage": "x", "done": "4", "total": 5}"#) == .badValue(event: "stage_progress", field: "done"))
        #expect(error(#"{"event": "stage_progress", "t": true, "stage": "x", "done": 4, "total": 5}"#) == .badValue(event: "stage_progress", field: "t"))
        #expect(error(#"{"event": "stage_end", "t": 1, "stage": "x", "status": "running"}"#) == .badValue(event: "stage_end", field: "status"))
        #expect(error(#"{"event": "run_start", "t": 0, "source": "s", "run_dir": "r", "stages": ["ingest"]}"#) == .badValue(event: "run_start", field: "stages"))
    }

    @Test func nonASCIITextSurvives() {
        let e = decode(#"{"event": "log", "t": 1, "stage": "asr", "message": "Grüße – été ✓"}"#)
        #expect(e == .log(LogEvent(t: 1, stage: "asr", message: "Grüße – été ✓")))
    }

    @Test func lineSplitterJoinsPiecesAndKeepsAnUnfinishedLastLine() {
        var s = LineSplitter()
        #expect(s.feed(Data("{\"a\":".utf8)).isEmpty)
        #expect(s.feed(Data("1}\n{\"b\"".utf8)) == ["{\"a\":1}"])
        #expect(s.feed(Data(":2}\n\n".utf8)) == ["{\"b\":2}", ""])
        let euro = Array("€\n".utf8)  // three bytes, split in the middle
        #expect(s.feed(Data(euro[0..<1])).isEmpty)
        #expect(s.feed(Data(euro[1...])) == ["€"])
        #expect(s.feed(Data("tail".utf8)).isEmpty)
        #expect(s.finish() == "tail")
        #expect(s.finish() == nil)
    }
}
