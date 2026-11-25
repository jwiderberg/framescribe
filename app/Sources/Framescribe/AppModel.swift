import AppKit
import AVFoundation
import FramescribeCore
import Observation
import UniformTypeIdentifiers

struct Notice: Identifiable, Equatable {
    let id = UUID()
    let text: String
}

/// The app's state: the queue, the health checks, the model download and the settings.
@MainActor @Observable
final class AppModel {
    static let shared = AppModel()

    let queue = JobQueue(launcher: PosixLauncher())
    let download = ModelDownload(launcher: PosixLauncher())
    let runtime: EmbeddedRuntime?
    private(set) var health: HealthState = .checking
    private(set) var commands: ToolCommands?
    private(set) var ffmpegPath: String?
    private(set) var ffprobePath: String?
    var notice: Notice?
    var selectedJobID: Job.ID?
    /// Opens (or brings forward) the main window; set by the window's content, used when the Dock icon is clicked.
    @ObservationIgnored var showMainWindow: (() -> Void)?
    var settings: AppSettings {
        didSet { if settings != oldValue { settings.save(to: defaults) } }
    }

    @ObservationIgnored private let defaults = UserDefaults.standard
    @ObservationIgnored private let home = FileManager.default.homeDirectoryForCurrentUser

    private init() {
        settings = AppSettings.load(from: UserDefaults.standard)
        runtime = EmbeddedRuntime.locate(bundleResources: Bundle.main.resourceURL)
        queue.inspect = AppModel.inspect
        queue.planner = { [unowned self] job in try self.plan(job) }
        queue.onFinished = { [unowned self] job in self.jobFinished(job) }
        download.onFinished = { [unowned self] _ in Task { await self.checkHealth() } }
        Task { await startUp() }
    }

    var outputFolder: URL { settings.outputFolderURL(home: home) }
    var hasRunningChild: Bool { queue.isBusy || download.isRunning }
    var selectedJob: Job? { queue.jobs.first { $0.id == selectedJobID } }

    // ---- start-up and health ---------------------------------------------------------------------------------

    private func startUp() async {
        guard let runtime else {
            health = .unavailable("This copy of the app has no embedded runtime (Contents/Resources/runtime). "
                                  + "Build the app with app/build_app.sh.")
            return
        }
        let processEnv = ProcessInfo.processInfo.environment
        let setup = await Task.detached { () -> (ToolCommands, String?, String?) in
            let login = LoginShell.read(shell: processEnv["SHELL"], baseEnvironment: processEnv)
            let dirs = SearchPath.directories(loginShellPATH: login["PATH"], processPATH: processEnv["PATH"])
            let locator = ToolLocator(directories: dirs)
            let env = ChildEnvironment.make(processEnvironment: processEnv, loginEnvironment: login,
                                            searchPATH: locator.searchPATH)
            return (ToolCommands(runtime: runtime, environment: env), locator.locate("ffmpeg"), locator.locate("ffprobe"))
        }.value
        commands = setup.0
        ffmpegPath = setup.1
        ffprobePath = setup.2
        await checkHealth()
    }

    func checkHealth() async {
        guard let commands else { return }
        health = .checking
        queue.canStart = false
        let spec = commands.doctor()
        let output = await Task.detached { CapturedRun.run(spec, timeout: 180) }.value
        if let output, let report = DoctorReport.decode(output.stdout) {
            health = .checked(report)
        } else {
            let why = output.map { $0.stderr.split(separator: "\n").suffix(3).joined(separator: " ") } ?? ""
            health = .unavailable("The health check of the embedded runtime did not answer. \(why)")
        }
        queue.canStart = health.allowsJobs
    }

    func downloadModel() {
        guard let commands, !download.isRunning else { return }
        download.start(commands.setup())
    }

    // ---- adding videos ---------------------------------------------------------------------------------------

    func add(_ urls: [URL]) {
        let result = queue.enqueue(urls)
        if !result.rejected.isEmpty {
            let names = result.rejected.map(\.reason).joined(separator: "; ")
            notice = Notice(text: "\(names). Framescribe takes movie files (mp4, mov, m4v, mkv, webm, …).")
        } else if !result.added.isEmpty {
            notice = nil
        }
        if let first = result.added.first, selectedJob == nil || selectedJob?.state.isFinished == true {
            selectedJobID = first.id
        }
        for job in result.added { probeDuration(job) }
    }

    func chooseVideos() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie] + VideoFiles.extensions.compactMap { UTType(filenameExtension: $0) }
        panel.message = "Choose one or more screen recordings"
        panel.prompt = "Add"
        if panel.runModal() == .OK { add(panel.urls) }
    }

    static func inspect(_ url: URL) -> VideoFiles.Verdict {
        let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .contentTypeKey, .isRegularFileKey])
        let exists = values != nil && FileManager.default.fileExists(atPath: url.path)
        let isMovie = values?.contentType?.conforms(to: .movie) ?? false
        return VideoFiles.check(url, exists: exists, isDirectory: values?.isDirectory ?? false, conformsToMovie: isMovie)
    }

    private func probeDuration(_ job: Job) {
        let url = job.video
        Task {
            let asset = AVURLAsset(url: url)
            if let time = try? await asset.load(.duration), time.isNumeric, job.duration == nil {
                job.duration = time.seconds
            }
        }
    }

    // ---- running -------------------------------------------------------------------------------------------------

    private func plan(_ job: Job) throws -> JobLaunchPlan {
        guard let commands else { throw PlanError.notReady }
        let folder = outputFolder
        let dir = OutputLocation.chooseRunDirectory(video: job.video, outputFolder: folder)
        return JobLaunchPlan(command: commands.run(video: job.video, outputFolder: folder, runDirectory: dir),
                             runDirectory: dir)
    }

    private enum PlanError: Error, CustomStringConvertible {
        case notReady
        var description: String { "the embedded runtime is not ready" }
    }

    private func jobFinished(_ job: Job) {
        if !NSApp.isActive { NSApp.requestUserAttention(.informationalRequest) }
    }

    func runAgain(_ job: Job) {
        queue.remove(job)
        add([job.video])
    }

    func stopEverything(completion: @escaping @MainActor () -> Void) {
        var pending = 2
        let one: @MainActor () -> Void = {
            pending -= 1
            if pending == 0 { completion() }
        }
        queue.stopAll(completion: one)
        download.stopAll(completion: one)
    }

    // ---- Finder and pasteboard -------------------------------------------------------------------------------

    func open(_ url: URL) { NSWorkspace.shared.open(url) }

    func reveal(_ url: URL) { NSWorkspace.shared.activateFileViewerSelecting([url]) }

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
