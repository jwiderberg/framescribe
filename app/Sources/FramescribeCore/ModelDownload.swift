import Darwin
import Foundation
import Observation

/// `framescribe setup --progress json`: the speech model download, started only by the user's click.
@MainActor @Observable
public final class ModelDownload {
    public private(set) var progress = SetupProgress()
    public private(set) var isRunning = false
    public private(set) var lastStderr: String?

    @ObservationIgnored public let launcher: any ProcessLauncher
    @ObservationIgnored public var scheduler: any Scheduler
    @ObservationIgnored public var stopPolicy: StopPolicy
    @ObservationIgnored public var onFinished: (@MainActor (ModelDownload) -> Void)?
    private var process: (any RunningProcess)?
    private var stopWaiters: [@MainActor () -> Void] = []

    public init(launcher: any ProcessLauncher, scheduler: any Scheduler = TaskScheduler(),
                stopPolicy: StopPolicy = StopPolicy(interruptGrace: 5, terminateGrace: 5)) {
        self.launcher = launcher
        self.scheduler = scheduler
        self.stopPolicy = stopPolicy
    }

    public func start(_ command: CommandSpec) {
        guard process == nil else { return }
        progress = SetupProgress()
        progress.phase = .running
        lastStderr = nil
        isRunning = true
        do {
            process = try launcher.launch(command, queue: .main, callbacks: ProcessCallbacks(
                stdoutLine: { [weak self] line in
                    MainActor.assumeIsolated {
                        if case let .success(event) = EventDecoder.decode(line) { self?.progress.apply(event) }
                    }
                },
                stderrLine: { [weak self] line in
                    MainActor.assumeIsolated {
                        let text = line.trimmingCharacters(in: .whitespaces)
                        if !text.isEmpty { self?.lastStderr = text }
                    }
                },
                exit: { [weak self] status in
                    MainActor.assumeIsolated { self?.finished(status) }
                }))
        } catch {
            isRunning = false
            progress.phase = .failed("cannot start the download: \(error)")
        }
    }

    public func stop() {
        guard let process else { return }
        process.signalGroup(SIGINT)
        scheduler.after(stopPolicy.interruptGrace) { [weak self] in
            guard let self, self.process === process else { return }
            process.signalGroup(SIGTERM)
            self.scheduler.after(self.stopPolicy.terminateGrace) { [weak self] in
                guard let self, self.process === process else { return }
                process.signalGroup(SIGKILL)
            }
        }
    }

    public func stopAll(completion: @escaping @MainActor () -> Void) {
        guard process != nil else {
            completion()
            return
        }
        stopWaiters.append(completion)
        stop()
    }

    private func finished(_ status: ProcessExit) {
        process = nil
        isRunning = false
        switch progress.phase {
        case .finished, .failed:
            break
        default:  // no setup_end: the child died
            progress.phase = status.isSuccess ? .finished : .failed(lastStderr ?? "the download stopped (\(status))")
        }
        if status.isSuccess == false, case .finished = progress.phase {
            progress.phase = .failed(lastStderr ?? "the download stopped (\(status))")
        }
        onFinished?(self)
        let waiters = stopWaiters
        stopWaiters = []
        waiters.forEach { $0() }
    }
}
