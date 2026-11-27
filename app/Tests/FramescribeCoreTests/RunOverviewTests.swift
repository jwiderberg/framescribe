import Foundation
import Testing
@testable import FramescribeCore

@Suite("Run overview")
struct RunOverviewTests {
    let t0 = Date(timeIntervalSince1970: 2_000_000)

    func progress(_ lines: [String]) -> RunProgress {
        var p = RunProgress()
        for line in lines {
            guard case let .success(event) = EventDecoder.decode(line) else { Issue.record("does not decode: \(line)"); continue }
            p.apply(event, at: t0)
        }
        return p
    }

    let start = #"{"event":"run_start","t":0,"source":"/v/a.mp4","duration":6769.29,"run_dir":"/o/a","stages":[{"name":"ingest","skipped_reason":null},{"name":"asr.parakeet","skipped_reason":null},{"name":"asr","skipped_reason":null},{"name":"visual","skipped_reason":null},{"name":"ocr","skipped_reason":null},{"name":"report","skipped_reason":null}]}"#

    func end(_ stage: String, _ status: String, _ wall: Double, _ summary: String) -> String {
        #"{"event":"stage_end","t":1,"stage":"\#(stage)","status":"\#(status)","wall_seconds":\#(wall),"summary":"\#(summary)"}"#
    }

    func runEnd(_ completeness: String) -> String {
        #"{"event":"run_end","t":486,"status":"ok","run_dir":"/o/a","wall_seconds":485.894,"outputs":[],"warnings":["x"],"completeness":\#(completeness)}"#
    }

    let figures = #"{"summary":"s","primary_words":10827,"filled_words":128,"filled_share":0.01168,"confirmed":8,"unconfirmed":59,"rejected":6,"limit":0.03,"above_limit":false,"warning":null}"#

    @Test func aFullRunGivesTimesCountsAndTheCompletenessLine() {
        let o = RunOverview(progress([
            start, end("ingest", "done", 7.09, "01:52:49, content"), end("asr.parakeet", "done", 203.27, "10827 words"),
            end("asr", "done", 1.31, "1528 segments"), end("visual", "done", 73.59, "763 states (6.8/min; 1 initial)"),
            end("ocr", "done", 200.24, "763 states, 763 frames read"), end("report", "done", 0.29, "54 steps, 36 pointer hints"),
            runEnd(figures),
        ]))
        #expect(o.rows.map(\.label) == ["Total time", "Speech recognition", "Screen changes", "Text on screen", "Report"])
        #expect(o.rows.map(\.value) == ["8 min 6 s for 1:52:49 of video", "3 min 25 s, 10,827 words", "1 min 14 s, 763 key frames",
                                        "3 min 20 s", "54 steps"])
        #expect(o.completeness == "128 words (1.2%) were filled in from other sources, under the 3% warning limit. "
                + "Of the 67 fills, 8 were confirmed by two sources and 59 were not. 6 candidates were rejected.")
    }

    @Test func cachedStagesSayFromTheCache() {
        let o = RunOverview(progress([
            start, end("asr.parakeet", "cached", 0, "10827 words"), end("asr", "cached", 0, "x"),
            end("visual", "cached", 0, "1 states"), end("ocr", "cached", 0, "x"), end("report", "done", 0.2, "1 steps"),
            runEnd(figures),
        ]))
        #expect(o.rows.first { $0.label == "Speech recognition" }?.value == "from the cache, 10,827 words")
        #expect(o.rows.first { $0.label == "Screen changes" }?.value == "from the cache, 1 key frame")
        #expect(o.rows.first { $0.label == "Report" }?.value == "1 step")
    }

    @Test func figuresTheToolDidNotReportAreLeftOut() {
        let o = RunOverview(progress([
            start, end("visual", "done", 5, "no states found"), end("report", "done", 0.2, "[link](x) steps"),
            #"{"event":"run_end","t":9,"status":"ok","run_dir":"/o/a","wall_seconds":9,"outputs":[],"warnings":[]}"#,
        ]))
        #expect(o.rows.map(\.label) == ["Total time", "Screen changes"])
        #expect(o.rows[1].value == "5 s")
        #expect(o.completeness == nil)
    }

    @Test func completenessWording() {
        func line(_ json: String) -> String? { RunOverview(progress([start, runEnd(json)])).completeness }
        #expect(line(#"{"summary":"s","filled_words":0}"#) == "Nothing had to be filled in from other sources.")
        #expect(line(#"{"summary":"s","filled_words":1,"filled_share":0.0004,"confirmed":1,"unconfirmed":0,"rejected":1,"limit":0.025,"above_limit":false}"#)
                == "1 word (0.0%) was filled in from other sources, under the 2.5% warning limit. The 1 fill was confirmed by two sources. 1 candidate was rejected.")
        #expect(line(#"{"summary":"s","filled_words":900,"filled_share":0.09,"limit":0.03,"above_limit":true}"#)
                == "900 words (9.0%) were filled in from other sources, above the 3% warning limit.")
        #expect(line(#"{"summary":"s","filled_words":900,"filled_share":0.09,"above_limit":true}"#)
                == "900 words (9.0%) were filled in from other sources, above the warning limit.")
        // one other transcript: nothing is called unconfirmed, since no second source existed
        #expect(line(#"{"summary":"s","sources":["parakeet-pass2"],"filled_words":14,"filled_share":0.006,"confirmed":0,"unconfirmed":4,"rejected":1,"limit":0.03,"above_limit":false}"#)
                == "14 words (0.6%) were filled in from other sources, under the 3% warning limit. The 4 fills come from the only other transcript, so no second source could confirm them. 1 candidate was rejected.")
        #expect(line(#"{"summary":"s"}"#) == nil)
    }

    @Test func impossibleCountsAreLeftOutAndNeverCrash() {
        func line(_ json: String) -> String? { RunOverview(progress([start, runEnd(json)])).completeness }
        #expect(line(#"{"summary":"s","filled_words":5,"confirmed":5e18,"unconfirmed":5e18,"rejected":-3}"#)
                == "5 words were filled in from other sources.")
        #expect(line(#"{"summary":"s","filled_words":-1}"#) == nil)
        let notice = progress([start, runEnd(#"{"summary":"s","filled_words":9e18,"primary_words":9e18,"filled_share":0.5,"above_limit":true}"#)])
            .completeness?.aboveLimitNotice
        #expect(notice?.contains("50.0% of the transcript") == true)
    }

    @Test func aCountIsReadOnlyBeforeItsOwnUnit() {
        #expect(RunOverview.leadingCount("12 frames", unit: "states") == nil)
        #expect(RunOverview.leadingCount("12 states,", unit: "states") == 12)
        #expect(RunOverview.leadingCount("12 states, 3 read", unit: "states") == 12)
        #expect(RunOverview.leadingCount("12", unit: "states") == nil)
        #expect(RunOverview.leadingCount("1234567890 states x", unit: "states") == nil)
    }

    @Test func partlyCachedSpeechSaysSo() {
        let o = RunOverview(progress([start, end("asr.parakeet", "cached", 0, "5 words"), end("asr", "done", 1.2, "x"), runEnd(figures)]))
        #expect(o.rows.first { $0.label == "Speech recognition" }?.value == "1 s, the rest from the cache, 10,827 words")
    }

    @Test func numbersAndSizes() {
        #expect(RunOverview.spoken(7.1) == "7 s")
        #expect(RunOverview.spoken(120) == "2 min")
        #expect(RunOverview.spoken(4000) == "1 h 6 min")
        #expect(RunOverview.spoken(-3) == "0 s")
        #expect(RunOverview.spoken(.infinity) == "0 s")
        #expect(RunOverview.grouped(999) == "999")
        #expect(RunOverview.grouped(1000) == "1,000")
        #expect(RunOverview.grouped(1234567) == "1,234,567")
        #expect(RunOverview.size(304_087_040) == "290 MB")
        #expect(RunOverview.size(3_221_225_472) == "3.0 GB")
        #expect(RunOverview.size(10) == "under 1 MB")
    }

    @Test func folderBytesCountsRegularFilesOnly() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("overview-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir.appendingPathComponent("sub"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        try Data(count: 100_000).write(to: dir.appendingPathComponent("sub/a.bin"))
        try FileManager.default.createSymbolicLink(at: dir.appendingPathComponent("link"), withDestinationURL: URL(fileURLWithPath: "/usr/bin"))
        let bytes = try #require(RunOverview.folderBytes(dir))
        #expect(bytes >= 100_000 && bytes < 1_000_000)
    }
}
