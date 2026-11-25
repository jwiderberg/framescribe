import Darwin
import Foundation

/// What to start: an executable, its arguments and its complete environment (nothing is inherited).
public struct CommandSpec: Sendable, Equatable {
    public var executable: String
    public var arguments: [String]
    public var environment: [String: String]
    public var currentDirectory: String?
    /// Give the child the read end of a pipe as file descriptor `lifelineFD`; the app holds the write end open for as
    /// long as it runs, so the child sees end-of-file when the app has gone away (also after a crash or force quit).
    public var lifeline: Bool

    public static let lifelineFD: Int32 = 3

    public init(executable: String, arguments: [String], environment: [String: String],
                currentDirectory: String? = nil, lifeline: Bool = false) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.currentDirectory = currentDirectory
        self.lifeline = lifeline
    }
}

public enum ProcessExit: Sendable, Equatable, CustomStringConvertible {
    case exited(Int32)
    case signaled(Int32)

    public var isSuccess: Bool { self == .exited(0) }

    public var description: String {
        switch self {
        case let .exited(code): return "exit status \(code)"
        case let .signaled(sig): return "stopped by signal \(sig) (\(String(cString: strsignal(sig))))"
        }
    }
}

/// A started child: the leader of its own process group, so a signal reaches everything it started.
public protocol RunningProcess: AnyObject, Sendable {
    var pid: Int32 { get }
    /// Send `signal` to the child's process group (no effect once it has exited).
    func signalGroup(_ signal: Int32)
}

public struct ProcessCallbacks: Sendable {
    public var stdoutLine: @Sendable (String) -> Void
    public var stderrLine: @Sendable (String) -> Void
    /// Called once, after the last stdout and stderr line.
    public var exit: @Sendable (ProcessExit) -> Void

    public init(stdoutLine: @escaping @Sendable (String) -> Void, stderrLine: @escaping @Sendable (String) -> Void,
                exit: @escaping @Sendable (ProcessExit) -> Void) {
        self.stdoutLine = stdoutLine
        self.stderrLine = stderrLine
        self.exit = exit
    }
}

public protocol ProcessLauncher: Sendable {
    /// Start `spec`. Callbacks arrive in order on `queue`.
    func launch(_ spec: CommandSpec, queue: DispatchQueue, callbacks: ProcessCallbacks) throws -> any RunningProcess
}

public struct LaunchError: Error, Sendable, Equatable, CustomStringConvertible {
    public let message: String
    public var description: String { message }
}

/// posix_spawn with: a new process group, default signal dispositions and an empty signal mask (so SIGINT stops a
/// Python child the way Ctrl-C does), stdin from /dev/null, stdout and stderr as pipes, and no other inherited file
/// descriptor except the optional lifeline.
public struct PosixLauncher: ProcessLauncher {
    public init() {}

    public func launch(_ spec: CommandSpec, queue: DispatchQueue, callbacks: ProcessCallbacks) throws -> any RunningProcess {
        var out: [Int32] = [-1, -1], err: [Int32] = [-1, -1], life: [Int32] = [-1, -1]
        guard pipe(&out) == 0, pipe(&err) == 0 else { throw LaunchError(message: "pipe: \(String(cString: strerror(errno)))") }
        if spec.lifeline, pipe(&life) != 0 { throw LaunchError(message: "pipe: \(String(cString: strerror(errno)))") }
        for fd in out + err + life where fd >= 0 { _ = fcntl(fd, F_SETFD, FD_CLOEXEC) }

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, out[1], 1)
        posix_spawn_file_actions_adddup2(&actions, err[1], 2)
        if spec.lifeline { posix_spawn_file_actions_adddup2(&actions, life[0], CommandSpec.lifelineFD) }
        if let dir = spec.currentDirectory { posix_spawn_file_actions_addchdir_np(&actions, dir) }

        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        let flags = POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_CLOEXEC_DEFAULT
        posix_spawnattr_setflags(&attr, Int16(flags))
        posix_spawnattr_setpgroup(&attr, 0)
        var all = sigset_t(), none = sigset_t()
        sigfillset(&all)
        sigemptyset(&none)
        posix_spawnattr_setsigdefault(&attr, &all)
        posix_spawnattr_setsigmask(&attr, &none)

        let argv = [spec.executable] + spec.arguments
        let envp = spec.environment.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
        var pid: pid_t = 0
        let rc = withCStrings(argv) { cArgv in
            withCStrings(envp) { cEnv in posix_spawn(&pid, spec.executable, &actions, &attr, cArgv, cEnv) }
        }
        close(out[1])
        close(err[1])
        if spec.lifeline { close(life[0]) }
        guard rc == 0 else {
            close(out[0]); close(err[0])
            if spec.lifeline { close(life[1]) }
            throw LaunchError(message: "cannot start \(spec.executable): \(String(cString: strerror(rc)))")
        }
        let process = PosixProcess(pid: pid, lifelineWriteFD: spec.lifeline ? life[1] : -1)
        process.watch(stdoutFD: out[0], stderrFD: err[0], queue: queue, callbacks: callbacks)
        return process
    }
}

final class PosixProcess: RunningProcess, @unchecked Sendable {
    let pid: Int32
    private let lock = NSLock()
    private var reaped = false
    private var lifelineWriteFD: Int32

    init(pid: Int32, lifelineWriteFD: Int32) {
        self.pid = pid
        self.lifelineWriteFD = lifelineWriteFD
    }

    func signalGroup(_ signal: Int32) {
        lock.lock()
        defer { lock.unlock() }
        if !reaped { _ = killpg(pid, signal) }
    }

    func watch(stdoutFD: Int32, stderrFD: Int32, queue: DispatchQueue, callbacks: ProcessCallbacks) {
        let streams = DispatchGroup()
        for (fd, deliver) in [(stdoutFD, callbacks.stdoutLine), (stderrFD, callbacks.stderrLine)] {
            streams.enter()
            let reader = Thread {
                var splitter = LineSplitter()
                var buffer = [UInt8](repeating: 0, count: 65536)
                while true {
                    let n = read(fd, &buffer, buffer.count)
                    if n < 0, errno == EINTR { continue }
                    if n <= 0 { break }
                    let lines = splitter.feed(Data(buffer[0..<n]))
                    if !lines.isEmpty { queue.async { lines.forEach(deliver) } }
                }
                if let last = splitter.finish() { queue.async { deliver(last) } }
                close(fd)
                streams.leave()
            }
            reader.name = "child \(pid) output"
            reader.start()
        }
        let waiter = Thread { [self] in
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0, errno == EINTR {}
            lock.lock()
            // whatever is left of the child's process group (a grandchild the child did not stop) goes with it
            _ = killpg(pid, SIGKILL)
            reaped = true
            if lifelineWriteFD >= 0 { close(lifelineWriteFD); lifelineWriteFD = -1 }
            lock.unlock()
            _ = streams.wait(timeout: .now() + 10)  // the pipes close when the last holder is gone
            let exit: ProcessExit = (status & 0x7f) == 0 ? .exited((status >> 8) & 0xff) : .signaled(status & 0x7f)
            queue.async { callbacks.exit(exit) }
        }
        waiter.name = "child \(pid) wait"
        waiter.start()
    }
}

private func withCStrings<R>(_ strings: [String], _ body: (UnsafePointer<UnsafeMutablePointer<CChar>?>) -> R) -> R {
    var pointers: [UnsafeMutablePointer<CChar>?] = strings.map { strdup($0) }
    pointers.append(nil)
    defer { pointers.forEach { free($0) } }
    return pointers.withUnsafeBufferPointer { body($0.baseAddress!) }
}

/// Runs a short command to completion and returns what it printed. For `doctor --json` and reading the login shell's
/// environment: never on the main thread.
public enum CapturedRun {
    public struct Output: Sendable, Equatable {
        public let status: ProcessExit?  // nil: timed out and stopped
        public let stdout: String
        public let stderr: String
    }

    public static func run(_ spec: CommandSpec, timeout: Double, launcher: any ProcessLauncher = PosixLauncher()) -> Output? {
        final class Box: @unchecked Sendable {
            var out: [String] = [], err: [String] = []
            var status: ProcessExit?
        }
        let box = Box()
        let done = DispatchSemaphore(value: 0)
        let queue = DispatchQueue(label: "captured-run")
        guard let process = try? launcher.launch(spec, queue: queue, callbacks: ProcessCallbacks(
            stdoutLine: { box.out.append($0) },
            stderrLine: { box.err.append($0) },
            exit: { box.status = $0; done.signal() }))
        else { return nil }
        var timedOut = false
        if done.wait(timeout: .now() + timeout) == .timedOut {
            timedOut = true
            process.signalGroup(SIGTERM)
            if done.wait(timeout: .now() + 2) == .timedOut {
                process.signalGroup(SIGKILL)
                done.wait()
            }
        }
        return queue.sync {
            Output(status: timedOut ? nil : box.status, stdout: box.out.joined(separator: "\n"),
                   stderr: box.err.joined(separator: "\n"))
        }
    }
}
