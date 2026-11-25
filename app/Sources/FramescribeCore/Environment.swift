import Foundation

/// The file system questions tool location asks; replaced in tests.
public protocol FileProbe: Sendable {
    func isExecutableFile(_ path: String) -> Bool
}

public struct RealFileProbe: FileProbe {
    public init() {}

    public func isExecutableFile(_ path: String) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDir) && !isDir.boolValue
            && FileManager.default.isExecutableFile(atPath: path)
    }
}

/// Where the app looks for ffmpeg and ffprobe. An app started from the Finder or the Dock does not get the shell's
/// PATH, so the login shell's PATH is read once and Homebrew's usual places are added.
public enum SearchPath {
    public static let homebrewDirectories = ["/opt/homebrew/bin", "/usr/local/bin"]
    public static let systemDirectories = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    /// The login shell's PATH first (the user's own order), then Homebrew's places, then the app's own PATH, then the
    /// system directories; each directory once, empty and relative entries dropped.
    public static func directories(loginShellPATH: String?, processPATH: String?) -> [String] {
        var seen = Set<String>()
        var result: [String] = []
        let candidates = split(loginShellPATH) + homebrewDirectories + split(processPATH) + systemDirectories
        for raw in candidates {
            let dir = raw.count > 1 && raw.hasSuffix("/") ? String(raw.dropLast()) : raw
            guard dir.hasPrefix("/"), seen.insert(dir).inserted else { continue }
            result.append(dir)
        }
        return result
    }

    static func split(_ path: String?) -> [String] {
        (path ?? "").split(separator: ":", omittingEmptySubsequences: true).map(String.init)
    }
}

public struct ToolLocator: Sendable {
    public let directories: [String]
    public let probe: any FileProbe

    public init(directories: [String], probe: any FileProbe = RealFileProbe()) {
        self.directories = directories
        self.probe = probe
    }

    /// The first executable called `name` in the search directories.
    public func locate(_ name: String) -> String? {
        for dir in directories {
            let candidate = (dir as NSString).appendingPathComponent(name)
            if probe.isExecutableFile(candidate) { return candidate }
        }
        return nil
    }

    public var searchPATH: String { directories.joined(separator: ":") }
}

/// Reading the environment of the user's login shell (PATH, and where the Hugging Face cache is if they moved it).
public enum LoginShell {
    static let begin = "__FRAMESCRIBE_ENV_BEGIN__"
    static let end = "__FRAMESCRIBE_ENV_END__"

    /// What to run: `$SHELL -l -c` printing the environment between two markers, so whatever the profile prints is
    /// ignored.
    public static func command(shell: String?, baseEnvironment: [String: String]) -> CommandSpec {
        let sh = (shell?.hasPrefix("/") == true) ? shell! : "/bin/zsh"
        let script = "printf '\\n\(begin)\\n'; /usr/bin/env; printf '\\n\(end)\\n'"
        return CommandSpec(executable: sh, arguments: ["-l", "-c", script], environment: baseEnvironment)
    }

    public static func parseEnvironment(_ output: String) -> [String: String] {
        let lines = output.components(separatedBy: "\n")
        guard let start = lines.lastIndex(of: begin) else { return [:] }
        let rest = lines[(start + 1)...]
        let stop = rest.firstIndex(of: end) ?? rest.endIndex
        var env: [String: String] = [:]
        for line in rest[..<stop] {
            guard let eq = line.firstIndex(of: "="), eq != line.startIndex else { continue }
            let key = String(line[..<eq])
            guard key.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" }) else { continue }  // continuation lines
            env[key] = String(line[line.index(after: eq)...])
        }
        return env
    }

    /// Runs the login shell (at most `timeout` seconds). Empty when it fails.
    public static func read(shell: String?, baseEnvironment: [String: String], timeout: Double = 8) -> [String: String] {
        guard let out = CapturedRun.run(command(shell: shell, baseEnvironment: baseEnvironment), timeout: timeout) else {
            return [:]
        }
        return parseEnvironment(out.stdout)
    }
}

/// The environment a framescribe child gets: built from scratch, never the app's whole environment.
public enum ChildEnvironment {
    /// Taken from the app's own environment.
    public static let passedThrough = ["HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "LANG", "LC_ALL", "LC_CTYPE"]
    /// Taken from the login shell when set there (it wins over the app's own value): the Hugging Face cache location,
    /// proxies for `setup`, and framescribe's own WhisperKit settings.
    public static let fromLoginShell = [
        "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME", "HF_ENDPOINT", "HF_TOKEN",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
        "FRAMESCRIBE_WHISPERKIT_CLI", "FRAMESCRIBE_WHISPERKIT_MODEL",
    ]

    public static func make(processEnvironment: [String: String], loginEnvironment: [String: String],
                            searchPATH: String) -> [String: String] {
        var env: [String: String] = [:]
        for key in passedThrough { if let v = processEnvironment[key] { env[key] = v } }
        for key in fromLoginShell {
            if let v = loginEnvironment[key] ?? processEnvironment[key], !v.isEmpty { env[key] = v }
        }
        env["PATH"] = searchPATH
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        env["PYTHONNOUSERSITE"] = "1"  // nothing from ~/.local
        env["PYTHONSAFEPATH"] = "1"  // `-m` does not put the working directory on sys.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // the bundle is signed: never write into it
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        return env
    }
}
