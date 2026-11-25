import Foundation

/// One line of `framescribe doctor --json`.
public struct DoctorCheck: Sendable, Equatable, Identifiable {
    public var id: String { name }
    public let name: String
    /// ok | missing | warning
    public let status: String
    public let detail: String
    public let fix: String?
    public let required: Bool

    public init(name: String, status: String, detail: String, fix: String? = nil, required: Bool = true) {
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix
        self.required = required
    }

    /// The same rule as `doctor.Check.blocking`.
    public var isBlocking: Bool { required && status != "ok" && status != "warning" }
}

public struct DoctorReport: Sendable, Equatable {
    public static let ffmpegChecks: Set<String> = ["ffmpeg", "ffprobe"]
    public static let modelCheck = "speech model"
    public static let homebrewCommand = "brew install ffmpeg"

    public let ok: Bool
    public let checks: [DoctorCheck]

    public init(ok: Bool, checks: [DoctorCheck]) {
        self.ok = ok
        self.checks = checks
    }

    public static func decode(_ text: String) -> DoctorReport? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let ok = object["ok"] as? Bool, let raw = object["checks"] as? [[String: Any]]
        else { return nil }
        let checks = raw.compactMap { c -> DoctorCheck? in
            guard let name = c["name"] as? String, let status = c["status"] as? String else { return nil }
            return DoctorCheck(name: name, status: status, detail: c["detail"] as? String ?? "",
                               fix: c["fix"] as? String, required: c["required"] as? Bool ?? true)
        }
        return DoctorReport(ok: ok, checks: checks)
    }

    public var blocking: [DoctorCheck] { checks.filter(\.isBlocking) }
    public var warnings: [DoctorCheck] { checks.filter { $0.status == "warning" } }
    public var ffmpegMissing: Bool { blocking.contains { Self.ffmpegChecks.contains($0.name) } }
    public var modelMissing: Bool { blocking.contains { $0.name == Self.modelCheck } }
    /// Blocking problems other than ffmpeg and the model, which have their own explanation and button.
    public var otherBlocking: [DoctorCheck] {
        blocking.filter { !Self.ffmpegChecks.contains($0.name) && $0.name != Self.modelCheck }
    }
    public var allowsJobs: Bool { ok && blocking.isEmpty }
}

/// What the app knows about whether jobs can run.
public enum HealthState: Sendable, Equatable {
    case checking
    case checked(DoctorReport)
    /// The embedded runtime is missing or did not answer.
    case unavailable(String)

    public var allowsJobs: Bool {
        if case let .checked(report) = self { return report.allowsJobs }
        return false
    }

    public var report: DoctorReport? {
        if case let .checked(report) = self { return report }
        return nil
    }
}

/// The speech model download (`setup --progress json`), as the window shows it.
public struct SetupProgress: Sendable, Equatable {
    public enum Phase: Sendable, Equatable { case idle, running, finished, failed(String) }

    public var phase: Phase = .idle
    public var files: [String] = []
    public var sizeGB: Double = 0
    public var bytes: [String: (done: Int64, total: Int64)] = [:]
    public var cacheDir: String?

    public init() {}

    public static func == (a: SetupProgress, b: SetupProgress) -> Bool {
        a.phase == b.phase && a.files == b.files && a.sizeGB == b.sizeGB && a.cacheDir == b.cacheDir
            && a.bytes.mapValues { [$0.done, $0.total] } == b.bytes.mapValues { [$0.done, $0.total] }
    }

    public mutating func apply(_ event: ToolEvent) {
        switch event {
        case let .setupStart(e):
            phase = .running
            files = e.files
            sizeGB = e.sizeGB
            cacheDir = e.cacheDir
        case let .setupProgress(e):
            phase = .running
            bytes[e.file] = (e.done, e.total)
        case let .setupEnd(e):
            switch e.status {
            case "ok", "present": phase = .finished
            default: phase = .failed(e.error ?? e.detail ?? e.status)
            }
        default:
            break
        }
    }

    public var doneBytes: Int64 { bytes.values.reduce(0) { $0 + $1.done } }

    /// The download's size: the files' totals once known, else the announced size.
    public var totalBytes: Int64 {
        let known = bytes.values.reduce(0) { $0 + $1.total }
        return max(known, Int64(sizeGB * 1e9))
    }

    public var fraction: Double? {
        let total = totalBytes
        return total > 0 ? min(1, Double(doneBytes) / Double(total)) : nil
    }
}
