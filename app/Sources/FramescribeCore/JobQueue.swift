import Darwin
import Foundation
import Observation

/// Waits, on the main actor. Replaced in tests so that the stop escalation can be stepped through.
@MainActor
public protocol Scheduler {
    func after(_ seconds: Double, _ work: @escaping @MainActor () -> Void)
}

public struct TaskScheduler: Scheduler {
    public init() {}

    public func after(_ seconds: Double, _ work: @escaping @MainActor () -> Void) {
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1e9))
            work()
        }
    }
}

/// How a running child is stopped: SIGINT to its process group (framescribe stops cleanly and keeps finished
/// stages cached), SIGTERM if it is still there after `interruptGrace`, SIGKILL after `terminateGrace` more.
public struct StopPolicy: Sendable, Equatable {
    public var interruptGrace: Double
    public var terminateGrace: Double

    public init(interruptGrace: Double = 20, terminateGrace: Double = 10) {
        self.interruptGrace = interruptGrace
        self.terminateGrace = terminateGrace
    }
}

/// What starting a job needs: the command, and the folder its results go to.
public struct JobLaunchPlan: Sendable, Equatable {
    public let command: CommandSpec
    public let runDirectory: URL

    public init(command: CommandSpec, runDirectory: URL) {
        self.command = command
        self.runDirectory = runDirectory
    }
}

public struct Rejection: Sendable, Equatable {
    public let file: URL
    public let reason: String
}

/// The queue: jobs run strictly one after the other, and only while `canStart` is true.
@MainActor @Observable
public final class JobQueue {
    public private(set) var jobs: [Job] = []
    /// Set when the health checks pass. Dropped files wait while it is false.
    public var canStart = false {
        didSet { if canStart { pump() } }
    }

    @ObservationIgnored public let launcher: any ProcessLauncher
    @ObservationIgnored public var stopPolicy: StopPolicy
    @ObservationIgnored public var scheduler: any Scheduler
    @ObservationIgnored public var now: @MainActor () -> Date = { Date() }
    /// The command and output folder for a job, read when it starts (so settings changed meanwhile apply).
    @ObservationIgnored public var planner: (@MainActor (Job) throws -> JobLaunchPlan)?
    /// Whether a dropped file is a video; the app adds the system's type information.
    @ObservationIgnored public var inspect: @MainActor (URL) -> VideoFiles.Verdict = JobQueue.inspectByName
    /// Called when a job has finished, after its state is final.
    @ObservationIgnored public var onFinished: (@MainActor (Job) -> Void)?
    /// Write the app's log of each job next to its results.
    @ObservationIgnored public var writesAppLog = true
    /// Whether a job's video is (still) there when its turn comes; replaced in tests.
    @ObservationIgnored public var videoExists: @MainActor (URL) -> Bool = { url in
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && !isDir.boolValue
    }

    private var current: (job: Job, process: any RunningProcess, log: JobLogWriter?)?
    private var stopWaiters: [@MainActor () -> Void] = []

    public init(launcher: any ProcessLauncher, stopPolicy: StopPolicy = StopPolicy(),
                scheduler: any Scheduler = TaskScheduler()) {
        self.launcher = launcher
        self.stopPolicy = stopPolicy
        self.scheduler = scheduler
    }

    public var runningJob: Job? { current?.job }
    public var isBusy: Bool { current != nil }
    public var waitingJobs: [Job] { jobs.filter { $0.state == .waiting } }

    public static func inspectByName(_ url: URL) -> VideoFiles.Verdict {
        var isDir: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
        return VideoFiles.check(url, exists: exists, isDirectory: isDir.boolValue, conformsToMovie: false)
    }

    /// Adds the videos among `urls` in their order; a file already waiting or running is not added twice.
    @discardableResult
    public func enqueue(_ urls: [URL]) -> (added: [Job], rejected: [Rejection]) {
        var added: [Job] = [], rejected: [Rejection] = []
        for url in urls {
            switch inspect(url) {
            case .notAFile:
                rejected.append(Rejection(file: url, reason: "\(url.lastPathComponent) is not a file"))
                continue
            case .notAVideo:
                rejected.append(Rejection(file: url, reason: "\(url.lastPathComponent) is not a video file"))
                continue
            case .video:
                break
            }
            let job = Job(video: url)
            if jobs.contains(where: { !$0.state.isFinished && $0.video == job.video }) { continue }
            jobs.append(job)
            added.append(job)
        }
        markSharedOutputs()
        pump()
        return (added, rejected)
    }

    /// Removes a waiting or finished job; a running one has to be stopped first.
    public func remove(_ job: Job) {
        guard job.state != .running else { return }
        jobs.removeAll { $0 === job }
        markSharedOutputs()
    }

    public func removeFinished() {
        jobs.removeAll { $0.state.isFinished }
        markSharedOutputs()
    }

    /// Stops the running job (SIGINT, then SIGTERM, then SIGKILL; see StopPolicy), or removes a waiting one.
    public func stop(_ job: Job) {
        if job.state == .waiting {
            remove(job)
            return
        }
        guard let current, current.job === job, !job.stopRequested else { return }
        job.requestStop()
        let process = current.process
        process.signalGroup(SIGINT)
        scheduler.after(stopPolicy.interruptGrace) { [weak self] in
            guard let self, self.current?.process === process else { return }
            process.signalGroup(SIGTERM)
            self.scheduler.after(self.stopPolicy.terminateGrace) { [weak self] in
                guard let self, self.current?.process === process else { return }
                process.signalGroup(SIGKILL)
            }
        }
    }

    /// For quitting: no further job starts, the running one is stopped, and `completion` runs once it has exited
    /// (at once when nothing runs).
    public func stopAll(completion: @escaping @MainActor () -> Void) {
        canStart = false
        guard let job = current?.job else {
            completion()
            return
        }
        stopWaiters.append(completion)
        stop(job)
    }

    // ---- running ------------------------------------------------------------------------------------------------

    private func pump() {
        guard canStart, current == nil, let job = jobs.first(where: { $0.state == .waiting }) else { return }
        guard let planner else { return }
        // nothing is created for a video that went away while it waited: no output folder holding only a log
        guard videoExists(job.video) else {
            job.failToStart("The video is no longer there: \(job.video.path)", kind: .videoMissing, at: now())
            onFinished?(job)
            pump()
            return
        }
        let plan: JobLaunchPlan
        do {
            plan = try planner(job)
        } catch {
            job.failToStart("cannot start: \(error)", at: now())
            onFinished?(job)
            pump()
            return
        }
        var log: JobLogWriter?
        if OutputLocation.isSymbolicLink(plan.runDirectory.path) {
            job.failToStart("refusing to use the output folder \(plan.runDirectory.path): it is a symbolic link", at: now())
            onFinished?(job)
            pump()
            return
        }
        do {
            try FileManager.default.createDirectory(at: plan.runDirectory, withIntermediateDirectories: true)
            if writesAppLog {
                log = JobLogWriter(url: OutputLocation.appLog(in: plan.runDirectory), command: plan.command, now: now())
            }
        } catch {
            job.failToStart("cannot create the output folder \(plan.runDirectory.path): \(error.localizedDescription)", at: now())
            onFinished?(job)
            pump()
            return
        }
        job.markStarted(runDirectory: plan.runDirectory, appLog: log?.url, at: now())
        do {
            let process = try launcher.launch(plan.command, queue: .main, callbacks: ProcessCallbacks(
                stdoutLine: { [weak self] line in
                    MainActor.assumeIsolated {
                        log?.write(line)
                        job.receive(stdoutLine: line, at: self?.now() ?? Date())
                    }
                },
                stderrLine: { line in
                    MainActor.assumeIsolated {
                        log?.write("stderr: " + line)
                        job.receive(stderrLine: line)
                    }
                },
                exit: { [weak self] status in
                    MainActor.assumeIsolated { self?.finished(job, status: status) }
                }))
            current = (job, process, log)
        } catch {
            log?.write("cannot start: \(error)")
            log?.close()
            job.failToStart("cannot start framescribe: \(error)", at: now())
            onFinished?(job)
            pump()
        }
    }

    private func finished(_ job: Job, status: ProcessExit) {
        guard let c = current, c.job === job else { return }
        c.log?.write("exit: \(status)")
        c.log?.close()
        current = nil
        job.finish(exit: status, at: now())
        onFinished?(job)
        let waiters = stopWaiters
        stopWaiters = []
        waiters.forEach { $0() }
        pump()
    }

    private func markSharedOutputs() {
        let active = jobs.filter { !$0.state.isFinished }
        for job in active {
            let stem = OutputLocation.runFolderName(of: job.name)
            let other = active.first { $0 !== job && OutputLocation.runFolderName(of: $0.name) == stem }
            job.noteSharedOutput(with: other?.video.path)
        }
    }
}

/// The app's log of one job: the command, every stdout line (the events) and every stderr line. A later run of the
/// same video appends to the same file after a dated separator line, so the earlier runs stay readable. The file is
/// opened with O_NOFOLLOW: a symbolic link planted at its name is never written through (there is no log then).
@MainActor
final class JobLogWriter {
    static let separatorPrefix = "===== run started "

    let url: URL
    private var handle: FileHandle?

    init?(url: URL, command: CommandSpec, now: Date = Date()) {
        var st = stat()
        let existed = lstat(url.path, &st) == 0
        if existed && (st.st_mode & S_IFMT) != S_IFREG { return nil }  // a link, a folder: never written through
        let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o644)
        guard fd >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        self.url = url
        self.handle = handle
        let stamp = ISO8601DateFormatter().string(from: now)
        if existed {
            write("")  // O_APPEND: every write goes to the end
        }
        write(Self.separatorPrefix + stamp + " =====")
        write("started: \(stamp)")
        write("command: " + ([command.executable] + command.arguments).joined(separator: " "))
    }

    func write(_ line: String) {
        try? handle?.write(contentsOf: Data((line + "\n").utf8))
    }

    func close() {
        try? handle?.close()
        handle = nil
    }
}
