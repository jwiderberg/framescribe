import Foundation

/// The state of one pipeline stage, as the event stream names it (plus `pending` and `running`, which the app derives).
public enum StageStatus: String, Sendable, Equatable, CaseIterable {
    case pending, running, cached, done, skipped, failed

    public var isFinished: Bool { self == .cached || self == .done || self == .skipped }
}

public struct StagePlan: Sendable, Equatable {
    public let name: String
    public let skippedReason: String?

    public init(name: String, skippedReason: String? = nil) {
        self.name = name
        self.skippedReason = skippedReason
    }
}

/// The completeness figures `run_end` carries (`completeness` in docs/DESIGN.md "Live status").
public struct CompletenessFigures: Sendable, Equatable {
    public let summary: String
    public let filledShare: Double?
    public let filledWords: Int?
    public let warning: String?
    /// Words of the primary speech recognition (the transcript has these plus the filled ones).
    public let primaryWords: Int?
    /// The run's limit (`--max-filled-share`), when the tool reports it.
    public let limit: Double?
    /// The filled share is above the limit: the transcript was kept, and the filled-in words should be checked.
    public let aboveLimit: Bool
    /// Fills whose every word two sources agree on, fills from one source only, and candidates not used.
    public var confirmed: Int? = nil
    public var unconfirmed: Int? = nil
    public var rejected: Int? = nil

    public init(summary: String, filledShare: Double? = nil, filledWords: Int? = nil, warning: String? = nil,
                primaryWords: Int? = nil, limit: Double? = nil, aboveLimit: Bool = false) {
        self.summary = summary
        self.filledShare = filledShare
        self.filledWords = filledWords
        self.warning = warning
        self.primaryWords = primaryWords
        self.limit = limit
        self.aboveLimit = aboveLimit
    }

    /// The line a finished job shows when the share is above the limit: how much was filled in, plainly. nil otherwise.
    public var aboveLimitNotice: String? {
        guard aboveLimit else { return nil }
        let share = filledShare.map { Self.percent($0) } ?? "a large share"
        var amount = "\(share) of the transcript"
        if let filled = filledWords, let primary = primaryWords {
            amount = "\(filled) of the transcript's \(primary + filled) words (\(share))"
        }
        let usual = limit.map { "more than the usual \(Self.percent($0))" } ?? "more than usual"
        return "\(amount) had to be filled in from other sources, \(usual): the speech recognition missed part of the "
            + "speech. The filled-in words are marked in the report; check them against the recording."
    }

    /// 0.0762 -> "7.6%"
    public static func percent(_ share: Double) -> String { String(format: "%.1f%%", share * 100) }
}

/// What kind of failure a `stage_end`/`run_end` reports (`error_kind`), for the cases the app explains itself.
public enum ToolErrorKind: Sendable, Equatable {
    /// The completeness gate refused the transcript (only with --strict-completeness, which the app never passes).
    case incompleteTranscript
    /// The video has no audio stream.
    case noAudio
    /// A kind this app does not know (a newer tool).
    case other(String)

    public init?(wire: String?) {
        switch wire {
        case nil: return nil
        case "incomplete_transcript": self = .incompleteTranscript
        case "no_audio": self = .noAudio
        case let s?: self = .other(s)
        }
    }
}

public struct RunStartEvent: Sendable, Equatable {
    public let t: Double
    public let source: String
    public let duration: Double?
    public let runDir: String
    public let stages: [StagePlan]
}

public struct StageProgressEvent: Sendable, Equatable {
    public let t: Double
    public let stage: String
    public let done: Double
    public let total: Double
    public let unit: String
    public let note: String?
}

public struct LogEvent: Sendable, Equatable {
    public let t: Double
    public let stage: String
    public let message: String
}

public struct StageEndEvent: Sendable, Equatable {
    public let t: Double
    public let stage: String
    public let status: StageStatus
    public let wallSeconds: Double
    public let summary: String?
    public let reason: String?
    public let error: String?
    public let logPath: String?
    public var errorKind: ToolErrorKind? = nil
}

public struct RunEndEvent: Sendable, Equatable {
    public let t: Double
    /// ok | failed | interrupted
    public let status: String
    public let runDir: String
    public let wallSeconds: Double
    public let error: String?
    public let outputs: [String]
    public let completeness: CompletenessFigures?
    public let warnings: [String]
    public var errorKind: ToolErrorKind? = nil
}

public struct SetupStartEvent: Sendable, Equatable {
    public let t: Double
    public let repo: String
    public let sizeGB: Double
    public let cacheDir: String
    public let freeGB: Double
    public let files: [String]
}

public struct SetupProgressEvent: Sendable, Equatable {
    public let t: Double
    public let file: String
    public let done: Int64
    public let total: Int64
}

public struct SetupEndEvent: Sendable, Equatable {
    public let t: Double
    /// ok | present | missing | failed | interrupted
    public let status: String
    public let detail: String?
    public let error: String?
}

/// One line of `framescribe run --progress json` or `setup --progress json`.
public enum ToolEvent: Sendable, Equatable {
    case runStart(RunStartEvent)
    case stageStart(t: Double, stage: String)
    case stageProgress(StageProgressEvent)
    case log(LogEvent)
    case stageEnd(StageEndEvent)
    case runEnd(RunEndEvent)
    case setupStart(SetupStartEvent)
    case setupProgress(SetupProgressEvent)
    case setupEnd(SetupEndEvent)
    /// A well-formed event of a kind this app does not know (a newer CLI): ignored, not an error.
    case unknown(kind: String)
}

public enum EventDecodeError: Error, Sendable, Equatable, CustomStringConvertible {
    case empty
    case notJSON
    case notAnObject
    case missingField(event: String?, field: String)
    case badValue(event: String, field: String)

    public var description: String {
        switch self {
        case .empty: return "empty line"
        case .notJSON: return "not JSON"
        case .notAnObject: return "not a JSON object"
        case let .missingField(event, field): return "\(event ?? "event") without \(field)"
        case let .badValue(event, field): return "\(event): unexpected value for \(field)"
        }
    }
}

public enum EventDecoder {
    public static func decode(_ line: String) -> Result<ToolEvent, EventDecodeError> {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .failure(.empty) }
        guard let data = trimmed.data(using: .utf8),
              let any = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        else { return .failure(.notJSON) }
        guard let object = any as? [String: Any] else { return .failure(.notAnObject) }
        do {
            return .success(try decode(Fields(object)))
        } catch let error as EventDecodeError {
            return .failure(error)
        } catch {
            return .failure(.notJSON)
        }
    }

    private static func decode(_ f: Fields) throws -> ToolEvent {
        let kind = try f.string("event")
        f.kind = kind
        switch kind {
        case "run_start":
            let stages = try f.array("stages").map { item -> StagePlan in
                guard let o = item as? [String: Any] else { throw EventDecodeError.badValue(event: kind, field: "stages") }
                let s = Fields(o, kind: kind)
                return StagePlan(name: try s.string("name"), skippedReason: s.optionalString("skipped_reason"))
            }
            return .runStart(RunStartEvent(t: try f.double("t"), source: try f.string("source"),
                                           duration: f.optionalDouble("duration"), runDir: try f.string("run_dir"),
                                           stages: stages))
        case "stage_start":
            return .stageStart(t: try f.double("t"), stage: try f.string("stage"))
        case "stage_progress":
            return .stageProgress(StageProgressEvent(t: try f.double("t"), stage: try f.string("stage"),
                                                     done: try f.double("done"), total: try f.double("total"),
                                                     unit: f.optionalString("unit") ?? "", note: f.optionalString("note")))
        case "log":
            return .log(LogEvent(t: try f.double("t"), stage: try f.string("stage"), message: try f.string("message")))
        case "stage_end":
            let raw = try f.string("status")
            guard let status = StageStatus(rawValue: raw), status != .pending, status != .running else {
                throw EventDecodeError.badValue(event: kind, field: "status")
            }
            return .stageEnd(StageEndEvent(t: try f.double("t"), stage: try f.string("stage"), status: status,
                                           wallSeconds: f.optionalDouble("wall_seconds") ?? 0,
                                           summary: f.optionalString("summary"), reason: f.optionalString("reason"),
                                           error: f.optionalString("error"), logPath: f.optionalString("log_path"),
                                           errorKind: ToolErrorKind(wire: f.optionalString("error_kind"))))
        case "run_end":
            var completeness: CompletenessFigures?
            if let c = f.optionalObject("completeness") {
                let cf = Fields(c, kind: kind)
                completeness = CompletenessFigures(summary: cf.optionalString("summary") ?? "",
                                                   filledShare: cf.optionalDouble("filled_share"),
                                                   filledWords: cf.optionalDouble("filled_words").flatMap(Checked.int),
                                                   warning: cf.optionalString("warning"),
                                                   primaryWords: cf.optionalDouble("primary_words").flatMap(Checked.int),
                                                   limit: cf.optionalDouble("limit"),
                                                   aboveLimit: cf.optionalBool("above_limit") ?? false)
                completeness?.confirmed = cf.optionalDouble("confirmed").flatMap(Checked.int)
                completeness?.unconfirmed = cf.optionalDouble("unconfirmed").flatMap(Checked.int)
                completeness?.rejected = cf.optionalDouble("rejected").flatMap(Checked.int)
            }
            return .runEnd(RunEndEvent(t: try f.double("t"), status: try f.string("status"),
                                       runDir: f.optionalString("run_dir") ?? "",
                                       wallSeconds: f.optionalDouble("wall_seconds") ?? 0,
                                       error: f.optionalString("error"),
                                       outputs: f.optionalStrings("outputs") ?? [],
                                       completeness: completeness, warnings: f.optionalStrings("warnings") ?? [],
                                       errorKind: ToolErrorKind(wire: f.optionalString("error_kind"))))
        case "setup_start":
            return .setupStart(SetupStartEvent(t: try f.double("t"), repo: f.optionalString("repo") ?? "",
                                               sizeGB: f.optionalDouble("size_gb") ?? 0,
                                               cacheDir: f.optionalString("cache_dir") ?? "",
                                               freeGB: f.optionalDouble("free_gb") ?? 0,
                                               files: f.optionalStrings("files") ?? []))
        case "setup_progress":
            guard let done = Checked.int64(try f.double("done")) else { throw EventDecodeError.badValue(event: kind, field: "done") }
            guard let total = Checked.int64(try f.double("total")) else { throw EventDecodeError.badValue(event: kind, field: "total") }
            return .setupProgress(SetupProgressEvent(t: try f.double("t"), file: try f.string("file"), done: done, total: total))
        case "setup_end":
            return .setupEnd(SetupEndEvent(t: try f.double("t"), status: try f.string("status"),
                                           detail: f.optionalString("detail"), error: f.optionalString("error")))
        default:
            return .unknown(kind: kind)
        }
    }
}

/// Typed access to a decoded JSON object, with errors that name the event and the field.
private final class Fields {
    let object: [String: Any]
    var kind: String?

    init(_ object: [String: Any], kind: String? = nil) {
        self.object = object
        self.kind = kind
    }

    private func present(_ key: String) -> Any? {
        guard let v = object[key], !(v is NSNull) else { return nil }
        return v
    }

    func string(_ key: String) throws -> String {
        guard let v = present(key) else { throw EventDecodeError.missingField(event: kind, field: key) }
        guard let s = v as? String else { throw EventDecodeError.badValue(event: kind ?? "event", field: key) }
        return s
    }

    func double(_ key: String) throws -> Double {
        guard let v = present(key) else { throw EventDecodeError.missingField(event: kind, field: key) }
        guard let n = Fields.number(v) else { throw EventDecodeError.badValue(event: kind ?? "event", field: key) }
        return n
    }

    func array(_ key: String) throws -> [Any] {
        guard let v = present(key) else { throw EventDecodeError.missingField(event: kind, field: key) }
        guard let a = v as? [Any] else { throw EventDecodeError.badValue(event: kind ?? "event", field: key) }
        return a
    }

    func optionalString(_ key: String) -> String? { present(key) as? String }
    func optionalBool(_ key: String) -> Bool? {
        guard let n = present(key) as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }
        return n.boolValue
    }
    func optionalDouble(_ key: String) -> Double? { present(key).flatMap(Fields.number) }
    func optionalObject(_ key: String) -> [String: Any]? { present(key) as? [String: Any] }
    func optionalStrings(_ key: String) -> [String]? { (present(key) as? [Any])?.compactMap { $0 as? String } }

    private static func number(_ v: Any) -> Double? {
        // JSONSerialization gives NSNumber for numbers and booleans; a boolean is not a number here
        guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        return n.doubleValue
    }
}

/// Splits a byte stream into lines. Feeds may end in the middle of a line (or of a UTF-8 sequence).
public struct LineSplitter: Sendable {
    private var buffer = Data()
    public init() {}

    public mutating func feed(_ data: Data) -> [String] {
        buffer.append(data)
        var lines: [String] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer[buffer.startIndex..<nl]
            lines.append(String(decoding: lineData, as: UTF8.self))
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        return lines
    }

    /// The last line when the stream ended without a newline.
    public mutating func finish() -> String? {
        defer { buffer.removeAll() }
        return buffer.isEmpty ? nil : String(decoding: buffer, as: UTF8.self)
    }
}
