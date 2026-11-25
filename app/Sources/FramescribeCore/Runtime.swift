import Foundation

/// The Python runtime inside the app bundle: `Contents/Resources/runtime`, a relocatable CPython 3.12 with
/// framescribe and its locked dependencies installed into its own site-packages.
public struct EmbeddedRuntime: Sendable, Equatable {
    public static let directoryName = "runtime"
    public static let pythonRelativePath = "bin/python3.12"
    /// Runs `framescribe.cli` like `python -m framescribe`, and stops the tool when the app's lifeline closes.
    public static let entryModule = "framescribe_app_launcher"
    public static let lifelineVariable = "FRAMESCRIBE_APP_LIFELINE_FD"

    public let root: URL

    public init(root: URL) { self.root = root }

    public var python: URL { root.appendingPathComponent(Self.pythonRelativePath) }

    public static func inBundle(resourcesDirectory: URL) -> EmbeddedRuntime {
        EmbeddedRuntime(root: resourcesDirectory.appendingPathComponent(directoryName, isDirectory: true))
    }

    /// The runtime of a bundle, or of the bundle that contains `executable` (for the command line driver).
    public static func locate(bundleResources: URL?, probe: any FileProbe = RealFileProbe()) -> EmbeddedRuntime? {
        guard let resources = bundleResources else { return nil }
        let runtime = inBundle(resourcesDirectory: resources)
        return probe.isExecutableFile(runtime.python.path) ? runtime : nil
    }

    /// `/path/Framescribe.app` -> its runtime
    public static func ofApp(_ app: URL) -> EmbeddedRuntime {
        inBundle(resourcesDirectory: app.appendingPathComponent("Contents/Resources", isDirectory: true))
    }
}

/// The command lines the app runs. Pure: everything comes in as values, so the tests can check every argument.
public struct ToolCommands: Sendable {
    public let runtime: EmbeddedRuntime
    public let environment: [String: String]

    public init(runtime: EmbeddedRuntime, environment: [String: String]) {
        self.runtime = runtime
        self.environment = environment
    }

    /// `run VIDEO --out FOLDER --run-dir RUNDIR --progress json`, then `extraArguments` (the command line driver's test
    /// hook; the app passes none). Paths are absolute, so a file name that starts with a dash is never read as an
    /// option. Never --strict-completeness: a transcript above the completeness limit is kept with a warning.
    public static func runArguments(video: URL, outputFolder: URL, runDirectory: URL,
                                    extraArguments: [String] = []) -> [String] {
        ["run", video.standardizedFileURL.path, "--out", outputFolder.standardizedFileURL.path,
         "--run-dir", runDirectory.standardizedFileURL.path, "--progress", "json"] + extraArguments
    }

    public func run(video: URL, outputFolder: URL, runDirectory: URL, extraArguments: [String] = []) -> CommandSpec {
        tool(Self.runArguments(video: video, outputFolder: outputFolder, runDirectory: runDirectory,
                               extraArguments: extraArguments), lifeline: true)
    }

    public func doctor() -> CommandSpec { tool(["doctor", "--json"], lifeline: true) }

    public func setup() -> CommandSpec { tool(["setup", "--progress", "json"], lifeline: true) }

    public func version() -> CommandSpec { tool(["--version"], lifeline: false) }

    func tool(_ arguments: [String], lifeline: Bool) -> CommandSpec {
        var env = environment
        if lifeline { env[EmbeddedRuntime.lifelineVariable] = String(CommandSpec.lifelineFD) }
        return CommandSpec(executable: runtime.python.path, arguments: ["-m", EmbeddedRuntime.entryModule] + arguments,
                           environment: env, currentDirectory: NSTemporaryDirectory(), lifeline: lifeline)
    }
}
