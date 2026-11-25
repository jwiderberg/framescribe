import Darwin
import Foundation
@testable import FramescribeCore

/// A process launcher that starts nothing: the test plays the child's part.
final class FakeLauncher: ProcessLauncher, @unchecked Sendable {
    final class Process: RunningProcess, @unchecked Sendable {
        let pid: Int32
        let spec: CommandSpec
        let callbacks: ProcessCallbacks
        private(set) var signals: [Int32] = []
        private(set) var exited = false

        init(pid: Int32, spec: CommandSpec, callbacks: ProcessCallbacks) {
            self.pid = pid
            self.spec = spec
            self.callbacks = callbacks
        }

        func signalGroup(_ signal: Int32) { if !exited { signals.append(signal) } }

        func say(_ lines: String...) { lines.forEach(callbacks.stdoutLine) }
        func say(lines: [String]) { lines.forEach(callbacks.stdoutLine) }
        func complain(_ line: String) { callbacks.stderrLine(line) }

        func exit(_ status: ProcessExit) {
            exited = true
            callbacks.exit(status)
        }
    }

    private(set) var launched: [Process] = []
    var failNextLaunch: String?

    func launch(_ spec: CommandSpec, queue: DispatchQueue, callbacks: ProcessCallbacks) throws -> any RunningProcess {
        if let message = failNextLaunch {
            failNextLaunch = nil
            throw LaunchError(message: message)
        }
        let p = Process(pid: Int32(1000 + launched.count), spec: spec, callbacks: callbacks)
        launched.append(p)
        return p
    }

    var last: Process { launched[launched.count - 1] }
}

/// A scheduler the test fires by hand.
@MainActor
final class ManualScheduler: Scheduler {
    private(set) var pending: [(seconds: Double, work: @MainActor () -> Void)] = []

    func after(_ seconds: Double, _ work: @escaping @MainActor () -> Void) { pending.append((seconds, work)) }

    /// Runs what is due now and returns the delays it had.
    @discardableResult
    func fire() -> [Double] {
        let due = pending
        pending = []
        due.forEach { $0.work() }
        return due.map(\.seconds)
    }
}

struct FakeProbe: FileProbe {
    let executables: Set<String>
    func isExecutableFile(_ path: String) -> Bool { executables.contains(path) }
}

enum Fixture {
    static func url(_ name: String) -> URL {
        Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures")!
    }

    static func text(_ name: String) -> String { try! String(contentsOf: url(name), encoding: .utf8) }

    static func lines(_ name: String) -> [String] {
        text(name).split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
    }

    static func events(_ name: String) -> [ToolEvent] {
        lines(name).map { line in
            guard case let .success(e) = EventDecoder.decode(line) else { fatalError("bad fixture line: \(line)") }
            return e
        }
    }
}

/// A temporary directory removed at the end of the test.
final class TempDir {
    let url: URL

    init() {
        url = FileManager.default.temporaryDirectory.appendingPathComponent("mt-tests-\(UUID().uuidString)", isDirectory: true)
        try! FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }

    deinit { try? FileManager.default.removeItem(at: url) }

    func file(_ name: String, contents: String = "x") -> URL {
        let u = url.appendingPathComponent(name)
        try! contents.write(to: u, atomically: true, encoding: .utf8)
        return u
    }
}

/// A queue wired to a fake launcher, with a planner like the app's.
@MainActor
func makeQueue(out: URL) -> (JobQueue, FakeLauncher, ManualScheduler) {
    let launcher = FakeLauncher()
    let scheduler = ManualScheduler()
    let queue = JobQueue(launcher: launcher, stopPolicy: StopPolicy(interruptGrace: 20, terminateGrace: 10),
                         scheduler: scheduler)
    let commands = ToolCommands(runtime: EmbeddedRuntime(root: URL(fileURLWithPath: "/App.app/Contents/Resources/runtime")),
                                environment: ["PATH": "/usr/bin"])
    queue.planner = { job in
        let dir = OutputLocation.chooseRunDirectory(video: job.video, outputFolder: out)
        return JobLaunchPlan(command: commands.run(video: job.video, outputFolder: out, runDirectory: dir), runDirectory: dir)
    }
    queue.writesAppLog = false
    return (queue, launcher, scheduler)
}
