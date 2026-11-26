import Foundation

/// What a finished job shows instead of the stages' notes: where the time went and what came out, in plain words.
/// Built only from the event stream (`RunProgress`). A figure the tool did not report is left out, never guessed.
public struct RunOverview: Sendable, Equatable {
    public struct Row: Sendable, Equatable, Identifiable {
        public let label: String
        public let value: String
        public var id: String { label }
    }

    public let rows: [Row]
    /// How much of the transcript was filled in from other sources, and how well supported. nil without a cross-check.
    public let completeness: String?

    public init(_ p: RunProgress) {
        var rows: [Row] = []
        if let wall = p.wallSeconds {
            let of = p.duration.map { " for \(Clock.format($0)) of video" } ?? ""
            rows.append(Row(label: "Total time", value: Self.spoken(wall) + of))
        }
        let speech = p.stages.filter { $0.name == "asr" || $0.name.hasPrefix("asr.") }
        if let v = Self.value(speech, detail: p.completeness?.primaryWords.map { "\(Self.grouped($0)) words" }) {
            rows.append(Row(label: "Speech recognition", value: v))
        }
        let visual = p.stages.filter { $0.name == "visual" }
        if let v = Self.value(visual, detail: Self.leadingCount(visual.first?.summary, unit: "states").map {
            "\(Self.grouped($0)) key \($0 == 1 ? "frame" : "frames")"
        }) {
            rows.append(Row(label: "Screen changes", value: v))
        }
        let ocr = p.stages.filter { $0.name == "ocr" }
        if let v = Self.value(ocr, detail: nil) { rows.append(Row(label: "Text on screen", value: v)) }
        if let steps = Self.leadingCount(p.stages.first { $0.name == "report" }?.summary, unit: "steps") {
            rows.append(Row(label: "Report", value: "\(Self.grouped(steps)) \(steps == 1 ? "step" : "steps")"))
        }
        self.rows = rows
        self.completeness = p.completeness.flatMap(Self.completenessLine)
    }

    /// "3 min 23 s, 10,827 words", "from the cache, 763 key frames", "1 s, the rest from the cache, 5 words". nil when
    /// none of the stages ran or was cached.
    static func value(_ stages: [StageView], detail: String?) -> String? {
        let ran = stages.filter { $0.status == .done }
        let cached = stages.contains { $0.status == .cached }
        guard !ran.isEmpty || cached else { return nil }
        var time = "from the cache"
        if !ran.isEmpty {
            time = spoken(ran.compactMap(\.wallSeconds).filter { $0.isFinite && $0 >= 0 }.reduce(0, +))
            if cached { time += ", the rest from the cache" }
        }
        return detail.map { "\(time), \($0)" } ?? time
    }

    /// "763 states (6.8/min; …)" with unit "states" -> 763. The summaries are text for people, so anything else is nil.
    static func leadingCount(_ summary: String?, unit: String) -> Int? {
        guard let parts = summary?.split(separator: " ", maxSplits: 2), parts.count >= 2,
              parts[0].count <= 9, parts[0].allSatisfy({ $0.isASCII && $0.isNumber }),
              parts[1].trimmingCharacters(in: CharacterSet(charactersIn: ",")) == unit else { return nil }
        return Int(parts[0])
    }

    static func completenessLine(_ c: CompletenessFigures) -> String? {
        guard let filled = c.filledWords else { return nil }
        if filled == 0 { return "Nothing had to be filled in from other sources." }
        var first = "\(grouped(filled)) \(filled == 1 ? "word was" : "words were") filled in from other sources"
        if let share = c.filledShare { first = "\(grouped(filled)) \(filled == 1 ? "word" : "words") (\(CompletenessFigures.percent(share))) \(filled == 1 ? "was" : "were") filled in from other sources" }
        if let limit = c.limit {
            first += c.aboveLimit ? ", above the \(Self.limitText(limit)) warning limit" : ", under the \(Self.limitText(limit)) warning limit"
        } else if c.aboveLimit {
            first += ", above the warning limit"
        }
        var line = first + "."
        if let confirmed = c.confirmed, let unconfirmed = c.unconfirmed, confirmed + unconfirmed > 0 {
            let total = confirmed + unconfirmed
            if c.sources == 1 {
                // one other transcript: nothing can be confirmed by a second, so do not report "0 confirmed"
                line += total == 1 ? " The 1 fill comes from the only other transcript, so no second source could confirm it."
                    : " The \(grouped(total)) fills come from the only other transcript, so no second source could confirm them."
            } else if total == 1 {
                line += confirmed == 1 ? " The 1 fill was confirmed by two sources." : " The 1 fill comes from one source only."
            } else {
                line += " Of the \(grouped(total)) fills, \(grouped(confirmed)) \(confirmed == 1 ? "was" : "were") confirmed by two sources and \(grouped(unconfirmed)) \(unconfirmed == 1 ? "comes" : "come") from one source only."
            }
        }
        if let rejected = c.rejected, rejected > 0 {
            line += " \(grouped(rejected)) \(rejected == 1 ? "candidate was" : "candidates were") rejected."
        }
        return line
    }

    /// 0.03 -> "3%", 0.025 -> "2.5%"
    static func limitText(_ limit: Double) -> String {
        let pct = limit * 100
        return abs(pct - pct.rounded()) < 0.05 ? String(format: "%.0f%%", pct) : String(format: "%.1f%%", pct)
    }

    /// 486 -> "8 min 6 s", 7.1 -> "7 s", 4000 -> "1 h 6 min"
    public static func spoken(_ seconds: Double) -> String {
        let s = max(0, Checked.int(seconds.rounded()) ?? 0)
        if s < 60 { return "\(s) s" }
        if s < 3600 { return s % 60 == 0 ? "\(s / 60) min" : "\(s / 60) min \(s % 60) s" }
        let m = (s % 3600) / 60
        return m == 0 ? "\(s / 3600) h" : "\(s / 3600) h \(m) min"
    }

    /// 10827 -> "10,827" (fixed grouping, so that tests and screenshots do not depend on the locale)
    public static func grouped(_ n: Int) -> String {
        let digits = String(abs(n))
        var out = ""
        for (i, ch) in digits.enumerated() {
            if i > 0, (digits.count - i) % 3 == 0 { out.append(",") }
            out.append(ch)
        }
        return n < 0 ? "-" + out : out
    }

    /// 304087040 -> "290 MB"
    public static func size(_ bytes: Int64) -> String {
        let mb = Double(bytes) / 1_048_576
        if mb < 1 { return "under 1 MB" }
        return mb < 1024 ? String(format: "%.0f MB", mb) : String(format: "%.1f GB", mb / 1024)
    }

    /// Bytes of the regular files under a folder, links not followed. nil when it cannot be read.
    public static func folderBytes(_ folder: URL) -> Int64? {
        let keys: [URLResourceKey] = [.isRegularFileKey, .totalFileAllocatedSizeKey]
        guard let walk = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: keys, options: []) else { return nil }
        var total: Int64 = 0
        for case let url as URL in walk {
            guard let v = try? url.resourceValues(forKeys: Set(keys)), v.isRegularFile == true else { continue }
            total += Int64(v.totalFileAllocatedSize ?? 0)
        }
        return total
    }
}
