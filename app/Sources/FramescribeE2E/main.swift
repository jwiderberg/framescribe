import Darwin
import Foundation
import FramescribeCore

// framescribe-e2e: runs a built app's embedded framescribe on one video through the same library code the app
// uses (login-shell PATH, child environment, command line, process launcher, event decoding, job queue), then checks
// what the app would show. No window.
//
//   framescribe-e2e --app build/Framescribe.app --video VIDEO --out FOLDER
//                      [--record EVENTS.jsonl] [--stop-in-stage STAGE] [--timeout SECONDS]
//                      [--max-filled-share SHARE --expect-above-limit]
//
// --stop-in-stage stops the job (as the Stop button does) once STAGE runs, and checks that it ends cancelled with no
// process of its group left. --max-filled-share is a test hook (the app has no such setting): it passes the limit to
// framescribe, so a clip can be made to exceed it; --expect-above-limit checks that the job is done and shows the
// above-limit warning. Exit status 0 when every check passes.

struct Options {
    var app: URL?
    var video: URL?
    var out: URL?
    var maxFilledShare: String?
    var expectAboveLimit = false
    var record: URL?
    var stopInStage: String?
    var timeout: Double = 900

    static func parse(_ args: [String]) -> Options? {
        var o = Options()
        var it = args.makeIterator()
        while let a = it.next() {
            switch a {
            case "--app": o.app = it.next().map { URL(fileURLWithPath: $0) }
            case "--video": o.video = it.next().map { URL(fileURLWithPath: $0) }
            case "--out": o.out = it.next().map { URL(fileURLWithPath: $0, isDirectory: true) }
            case "--record": o.record = it.next().map { URL(fileURLWithPath: $0) }
            case "--stop-in-stage": o.stopInStage = it.next()
            case "--timeout": o.timeout = it.next().flatMap(Double.init) ?? o.timeout
            case "--max-filled-share": o.maxFilledShare = it.next()
            case "--expect-above-limit": o.expectAboveLimit = true
            default: return nil
            }
        }
        return o.app != nil && o.video != nil && o.out != nil ? o : nil
    }
}

/// The real launcher, plus a copy of every stdout line in a file and the pid of the last child.
final class RecordingLauncher: ProcessLauncher, @unchecked Sendable {
    let inner = PosixLauncher()
    let record: FileHandle?
    var lastPID: Int32 = 0

    init(record: URL?) {
        if let record {
            FileManager.default.createFile(atPath: record.path, contents: nil)
            self.record = try? FileHandle(forWritingTo: record)
        } else {
            self.record = nil
        }
    }

    func launch(_ spec: CommandSpec, queue: DispatchQueue, callbacks: ProcessCallbacks) throws -> any RunningProcess {
        var cb = callbacks
        let record = self.record
        let original = callbacks.stdoutLine
        cb.stdoutLine = { line in
            try? record?.write(contentsOf: Data((line + "\n").utf8))
            original(line)
        }
        let p = try inner.launch(spec, queue: queue, callbacks: cb)
        lastPID = p.pid
        return p
    }
}

func say(_ s: String) { print(s); fflush(stdout) }

/// Memory seen while the job ran: the child's largest footprint (what Activity Monitor shows) and the lowest system
/// free-memory percentage (`kern.memorystatus_level`, the figure `memory_pressure` prints).
@MainActor final class MemorySamples {
    var peakChildMB: Double = 0
    var lowestFreePercent: Int32 = 100

    func sample(pid: Int32) {
        if pid > 0 {
            var info = rusage_info_v4()
            let rc = withUnsafeMutablePointer(to: &info) { ptr in
                ptr.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V4, $0) }
            }
            if rc == 0 { peakChildMB = max(peakChildMB, Double(info.ri_lifetime_max_phys_footprint) / 1_048_576) }
        }
        var level: Int32 = 0
        var size = MemoryLayout<Int32>.size
        if sysctlbyname("kern.memorystatus_level", &level, &size, nil, 0) == 0 { lowestFreePercent = min(lowestFreePercent, level) }
    }
}

@MainActor
func main() async -> Int32 {
    guard let o = Options.parse(Array(CommandLine.arguments.dropFirst())) else {
        say("usage: framescribe-e2e --app APP --video VIDEO --out FOLDER [--record FILE] [--stop-in-stage STAGE] [--timeout S] [--max-filled-share SHARE --expect-above-limit]")
        return 2
    }
    let runtime = EmbeddedRuntime.ofApp(o.app!)
    guard RealFileProbe().isExecutableFile(runtime.python.path) else {
        say("FAIL no embedded runtime at \(runtime.python.path)")
        return 1
    }
    let processEnv = ProcessInfo.processInfo.environment
    let login = LoginShell.read(shell: processEnv["SHELL"], baseEnvironment: processEnv)
    let locator = ToolLocator(directories: SearchPath.directories(loginShellPATH: login["PATH"], processPATH: processEnv["PATH"]))
    let env = ChildEnvironment.make(processEnvironment: processEnv, loginEnvironment: login, searchPATH: locator.searchPATH)
    let commands = ToolCommands(runtime: runtime, environment: env)
    say("ffmpeg: \(locator.locate("ffmpeg") ?? "not found"), ffprobe: \(locator.locate("ffprobe") ?? "not found")")

    let doctor = CapturedRun.run(commands.doctor(), timeout: 180)
    guard let doctorOut = doctor, let report = DoctorReport.decode(doctorOut.stdout) else {
        say("FAIL doctor --json did not answer: \(doctor?.stderr ?? "")")
        return 1
    }
    say("doctor: ok=\(report.ok) blocking=\(report.blocking.map(\.name))")
    guard report.allowsJobs else { return 1 }

    let launcher = RecordingLauncher(record: o.record)
    let queue = JobQueue(launcher: launcher)
    let extra = o.maxFilledShare.map { ["--max-filled-share", $0] } ?? []
    queue.planner = { job in  // as the app plans a job
        let dir = OutputLocation.chooseRunDirectory(video: job.video, outputFolder: o.out!)
        return JobLaunchPlan(command: commands.run(video: job.video, outputFolder: o.out!, runDirectory: dir,
                                                   extraArguments: extra), runDirectory: dir)
    }
    let t0 = Date()
    let added = queue.enqueue([o.video!])
    guard let job = added.added.first else {
        say("FAIL not queued: \(added.rejected.map(\.reason))")
        return 1
    }

    let memory = MemorySamples()
    let watcher = Task { @MainActor in
        var lastStage = ""
        var stopSent = false
        var timedOut = false
        while !Task.isCancelled {
            if !timedOut, Date().timeIntervalSince(t0) > o.timeout {
                timedOut = true
                say("timeout: stopping")
                queue.stop(job)
            }
            if let s = job.progress.runningStage {
                if s.name != lastStage {
                    lastStage = s.name
                    say(String(format: "%7.1fs  stage %@ running", Date().timeIntervalSince(t0), s.name))
                }
                if let stage = o.stopInStage, s.name == stage, !stopSent {
                    stopSent = true
                    say(String(format: "%7.1fs  stopping (SIGINT to the process group)", Date().timeIntervalSince(t0)))
                    queue.stop(job)
                }
            }
            if job.state == .running { memory.sample(pid: launcher.lastPID) }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
    }
    await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
        queue.onFinished = { _ in continuation.resume() }
        queue.canStart = true  // starts the job
        say("run folder: \(job.runDirectory?.path ?? "-")")
    }
    watcher.cancel()
    let wall = Date().timeIntervalSince(t0)

    var failures: [String] = []
    func check(_ ok: Bool, _ what: String) {
        say("\(ok ? "PASS" : "FAIL") \(what)")
        if !ok { failures.append(what) }
    }
    let p = job.progress
    let stagesText = p.stages.map { "\($0.name)=\($0.status.rawValue)" }.joined(separator: " ")
    say(String(format: "finished in %.1f s: state=%@ exit=%@", wall, job.state.rawValue, job.exitStatus.map { "\($0)" } ?? "-"))
    say("stages: \(stagesText)")
    say(String(format: "memory: framescribe peak footprint %.0f MB; lowest system free memory %d%%", memory.peakChildMB, memory.lowestFreePercent))
    if let c = p.completeness { say("completeness: \(c.summary)") }
    say("warnings: \(p.warnings.count), log lines: \(p.logLines.count), malformed stdout lines: \(p.malformedLines)")
    check(p.malformedLines == 0, "every stdout line is an event")
    check(p.hasStarted && !p.stages.isEmpty, "run_start arrived with the stage list")
    check(killpg(launcher.lastPID, 0) == -1 && errno == ESRCH, "no process of the child's group is left")
    if o.stopInStage != nil {
        check(job.state == .cancelled, "the job ended cancelled")
        check(p.endStatus == "interrupted", "run_end says interrupted")
        check(job.exitStatus == .exited(130), "framescribe exited 130 after SIGINT")
        let ingest = p.stages.first { $0.name == "ingest" }
        check(ingest?.status == .done || ingest?.status == .cached, "stages before the stop finished")
    } else {
        check(job.state == .done, "the job is done")
        check(p.endStatus == "ok", "run_end says ok")
        check(p.stages.allSatisfy { $0.status.isFinished }, "every stage finished (done, cached or skipped)")
        let report = job.report
        check(report.map { FileManager.default.fileExists(atPath: $0.path) } ?? false, "report.md exists: \(report?.path ?? "-")")
        if let notice = job.completenessNotice { say("window shows: \(notice)") }
        if o.expectAboveLimit {
            check(p.completeness?.aboveLimit == true, "run_end says above_limit")
            check(job.completenessNotice != nil, "the done job shows the above-limit warning")
            let head = (try? String(contentsOf: job.runDirectory!.appendingPathComponent("transcript.md"), encoding: .utf8))
                .map { String($0.prefix(2000)) } ?? ""
            check(head.contains("ABOVE THE USUAL LEVEL"), "transcript.md starts with the above-limit warning")
        }
        if let dir = job.runDirectory {
            let frames = (try? FileManager.default.contentsOfDirectory(atPath: dir.appendingPathComponent("frames").path)) ?? []
            check(!frames.isEmpty, "frames/ has \(frames.count) key frames")
            for f in ["timeline.json", "ANALYSIS_PROMPT.md", "transcript.md", "run.json", "framescribe-app.log"] {
                check(FileManager.default.fileExists(atPath: dir.appendingPathComponent(f).path), "\(f) exists")
            }
        }
    }
    if let failure = job.failure { say("failure: stage=\(failure.stage ?? "-") \(failure.message)") }
    say(failures.isEmpty ? "ALL CHECKS PASSED" : "\(failures.count) CHECK(S) FAILED")
    return failures.isEmpty ? 0 : 1
}

let status = await main()
exit(status)
