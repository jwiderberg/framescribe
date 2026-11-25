import Darwin
import Foundation

/// Which dropped files are videos. Matched on the file name extension, the same list Info.plist declares; the app
/// also accepts any file whose type conforms to public.movie.
public enum VideoFiles {
    public static let extensions: Set<String> = [
        "mp4", "mov", "m4v", "mkv", "webm", "avi", "mpg", "mpeg", "ts", "mts", "m2ts", "wmv", "flv", "3gp",
    ]

    public static func hasVideoExtension(_ url: URL) -> Bool {
        extensions.contains(url.pathExtension.lowercased())
    }

    public enum Verdict: Equatable, Sendable {
        case video
        case notAFile
        case notAVideo
    }

    /// `isDirectory`/`exists` come from the caller (the file system in the app, plain values in tests).
    public static func check(_ url: URL, exists: Bool, isDirectory: Bool, conformsToMovie: Bool) -> Verdict {
        guard url.isFileURL, exists, !isDirectory else { return .notAFile }
        return hasVideoExtension(url) || conformsToMovie ? .video : .notAVideo
    }
}

/// Where results go: `<output folder>/<video name without extension>/` (Python's `Path.stem`: the last extension only),
/// or `<stem> 2`, `<stem> 3`, … when that folder holds the run of another video of the same name
/// (chooseRunDirectory). The app passes the folder it chose with `--run-dir`.
public enum OutputLocation {
    public static let folderName = "framescribe"

    /// ~/Documents/framescribe
    public static func defaultFolder(home: URL) -> URL {
        home.appendingPathComponent("Documents", isDirectory: true).appendingPathComponent(folderName, isDirectory: true)
    }

    /// Python's `Path(name).stem`: "a.b.mp4" -> "a.b", ".mp4" -> ".mp4", "talk" -> "talk".
    public static func stem(of fileName: String) -> String {
        guard let dot = fileName.lastIndex(of: "."), dot != fileName.startIndex,
              fileName.index(after: dot) != fileName.endIndex
        else { return fileName }
        return String(fileName[..<dot])
    }

    public static func runDirectory(video: URL, outputFolder: URL) -> URL {
        outputFolder.appendingPathComponent(stem(of: video.lastPathComponent), isDirectory: true)
    }

    /// The folder for `video`'s results: `<stem>`, unless that folder already holds a run (its `run.json`) of a
    /// different source file; then the first of `<stem> 2`, `<stem> 3`, … that holds no run or a run of this video.
    /// The same video run again keeps its folder, so its finished stages are reused. `recordedSource` reads the source
    /// path a folder's run.json names (nil: no run.json; "" when it cannot be read: treated as another video's).
    public static func chooseRunDirectory(video: URL, outputFolder: URL,
                                          recordedSource: (URL) -> String? = OutputLocation.recordedSource(in:),
                                          canonical: (String) -> String = OutputLocation.canonicalPath) -> URL {
        let base = stem(of: video.lastPathComponent)
        let mine = canonical(video.standardizedFileURL.path)
        var n = 1
        while true {
            let dir = outputFolder.appendingPathComponent(n == 1 ? base : "\(base) \(n)", isDirectory: true)
            guard let source = recordedSource(dir) else { return dir }
            if !source.isEmpty, canonical(source) == mine { return dir }
            n += 1
        }
    }

    /// The `source` a folder's run.json records: nil without a run.json, "" when it has none or cannot be read.
    public static func recordedSource(in runDirectory: URL) -> String? {
        let file = runDirectory.appendingPathComponent("run.json")
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        guard let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let source = object["source"] as? String
        else { return "" }
        return source
    }

    /// The path with symbolic links resolved (`realpath`), as framescribe records the source; unchanged when the
    /// file is gone.
    public static func canonicalPath(_ path: String) -> String {
        guard let resolved = realpath(path, nil) else { return path }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    /// The folder in settings, or the default when it is unset or not an absolute path.
    public static func resolve(setting: String?, home: URL) -> URL {
        if let s = setting?.trimmingCharacters(in: .whitespaces), !s.isEmpty {
            let expanded = (s as NSString).expandingTildeInPath
            if expanded.hasPrefix("/") { return URL(fileURLWithPath: expanded, isDirectory: true).standardizedFileURL }
        }
        return defaultFolder(home: home)
    }

    /// The report of a finished run.
    public static func report(in runDirectory: URL) -> URL { runDirectory.appendingPathComponent("report.md") }

    /// The app's own log of a job (the child's stderr and the raw events), next to the results. Each run of the job
    /// appends to it, after a dated separator line.
    public static func appLog(in runDirectory: URL) -> URL { runDirectory.appendingPathComponent("framescribe-app.log") }
}

/// The app's settings, in its own defaults domain.
public struct AppSettings: Equatable, Sendable {
    public static let outputFolderKey = "outputFolder"
    /// Written by earlier versions ("Allow incomplete transcripts"); no longer read or written: a transcript above the
    /// completeness limit is always kept, with a warning.
    public static let retiredKeys = ["allowIncomplete"]

    /// nil: the default folder
    public var outputFolder: String?

    public init(outputFolder: String? = nil) {
        self.outputFolder = outputFolder
    }

    public static func load(from defaults: any SettingsStore) -> AppSettings {
        AppSettings(outputFolder: defaults.string(forKey: outputFolderKey))
    }

    public func save(to defaults: any SettingsStore) {
        if let folder = outputFolder { defaults.set(folder, forKey: Self.outputFolderKey) }
        else { defaults.removeObject(forKey: Self.outputFolderKey) }
    }

    public func outputFolderURL(home: URL) -> URL { OutputLocation.resolve(setting: outputFolder, home: home) }
}

/// Where settings are kept: UserDefaults in the app (its own defaults domain), a dictionary in the tests.
public protocol SettingsStore: AnyObject {
    func string(forKey key: String) -> String?
    func bool(forKey key: String) -> Bool
    func set(_ value: Any?, forKey key: String)
    func removeObject(forKey key: String)
}

extension UserDefaults: SettingsStore {}
