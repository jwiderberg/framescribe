import Darwin
import Foundation
import Testing
@testable import FramescribeCore

@Suite("Health checks and the model download")
struct HealthTests {
    @Test func aMissingModelBlocksJobsAndIsRecognised() throws {
        let report = try #require(DoctorReport.decode(Fixture.text("doctor_model_missing.json")))
        #expect(report.checks.count == 9 && !report.ok)
        #expect(report.modelMissing && !report.ffmpegMissing && report.otherBlocking.isEmpty)
        #expect(report.warnings.map(\.name) == ["memory"])
        #expect(!report.allowsJobs && !HealthState.checked(report).allowsJobs)
    }

    @Test func missingFfmpegIsRecognised() throws {
        let json = """
        {"ok": false, "checks": [
          {"name": "ffmpeg", "status": "missing", "detail": "not found on PATH", "fix": "brew install ffmpeg", "required": true},
          {"name": "ffprobe", "status": "missing", "detail": "not found on PATH", "fix": "brew install ffmpeg", "required": true},
          {"name": "platform", "status": "missing", "detail": "Linux x86_64", "fix": "use a Mac", "required": true},
          {"name": "disk space", "status": "warning", "detail": "1 GB free", "fix": null, "required": false}]}
        """
        let report = try #require(DoctorReport.decode(json))
        #expect(report.ffmpegMissing && !report.modelMissing && report.otherBlocking.map(\.name) == ["platform"])
        #expect(DoctorReport.homebrewCommand == "brew install ffmpeg")
    }

    @Test func everythingPresentAllowsJobs() throws {
        let json = #"{"ok": true, "checks": [{"name": "memory", "status": "warning", "detail": "12% free", "fix": "close apps", "required": false}]}"#
        let report = try #require(DoctorReport.decode(json))
        #expect(report.allowsJobs && HealthState.checked(report).allowsJobs)
        #expect(!HealthState.checking.allowsJobs && !HealthState.unavailable("x").allowsJobs)
    }

    @Test func notDoctorOutput() {
        #expect(DoctorReport.decode("error: something") == nil)
        #expect(DoctorReport.decode(#"{"checks": []}"#) == nil)
    }

    @Test func downloadProgressAddsUpTheFiles() {
        var p = SetupProgress()
        p.apply(.setupStart(SetupStartEvent(t: 0, repo: "r", sizeGB: 2.5, cacheDir: "/c", freeGB: 40, files: ["config.json", "model.safetensors"])))
        #expect(p.phase == .running && p.totalBytes == 2_500_000_000 && p.fraction == 0)
        p.apply(.setupProgress(SetupProgressEvent(t: 1, file: "config.json", done: 1000, total: 1000)))
        p.apply(.setupProgress(SetupProgressEvent(t: 2, file: "model.safetensors", done: 1_254_000_000, total: 2_508_000_000)))
        #expect(p.doneBytes == 1_254_001_000 && p.totalBytes == 2_508_001_000)
        #expect(abs((p.fraction ?? 0) - 0.5) < 0.001)
        p.apply(.setupEnd(SetupEndEvent(t: 3, status: "ok", detail: "ready", error: nil)))
        #expect(p.phase == .finished)
        var failed = SetupProgress()
        failed.apply(.setupEnd(SetupEndEvent(t: 0, status: "failed", detail: nil, error: "no route to host")))
        #expect(failed.phase == .failed("no route to host"))
    }

    @MainActor @Test func theDownloadRunsSetupAndCanBeStopped() {
        let launcher = FakeLauncher()
        let scheduler = ManualScheduler()
        let download = ModelDownload(launcher: launcher, scheduler: scheduler)
        var finished = 0
        download.onFinished = { _ in finished += 1 }
        let commands = ToolCommands(runtime: EmbeddedRuntime(root: URL(fileURLWithPath: "/r")), environment: [:])
        download.start(commands.setup())
        #expect(download.isRunning && launcher.last.spec.arguments.suffix(3) == ["setup", "--progress", "json"])
        download.start(commands.setup())  // one at a time
        #expect(launcher.launched.count == 1)
        launcher.last.say(#"{"event": "setup_start", "t": 0, "repo": "r", "size_gb": 2.5, "cache_dir": "/c", "free_gb": 40, "files": ["model.safetensors"]}"#,
                          #"{"event": "setup_progress", "t": 1, "file": "model.safetensors", "done": 500000000, "total": 2500000000}"#)
        #expect(abs((download.progress.fraction ?? 0) - 0.2) < 0.001)
        download.stop()
        #expect(launcher.last.signals == [SIGINT])
        scheduler.fire()
        #expect(launcher.last.signals == [SIGINT, SIGTERM])
        launcher.last.say(#"{"event": "setup_end", "t": 2, "status": "interrupted", "detail": null, "error": "interrupted"}"#)
        launcher.last.exit(.exited(130))
        #expect(!download.isRunning && download.progress.phase == .failed("interrupted") && finished == 1)
    }

    @MainActor @Test func aDownloadThatDiesWithoutAnEventIsAFailure() {
        let launcher = FakeLauncher()
        let download = ModelDownload(launcher: launcher, scheduler: ManualScheduler())
        download.start(CommandSpec(executable: "/x", arguments: [], environment: [:]))
        launcher.last.complain("Traceback: MemoryError")
        launcher.last.exit(.signaled(9))
        #expect(download.progress.phase == .failed("Traceback: MemoryError"))
    }
}

/// The real launcher, on /bin/sh: small processes, no model.
@Suite("Child processes", .serialized)
struct PosixLauncherTests {
    final class Collected: @unchecked Sendable {
        var out: [String] = [], err: [String] = []
        var status: ProcessExit?
        let done = DispatchSemaphore(value: 0)
    }

    func start(_ script: String, lifeline: Bool = false, env: [String: String] = ["PATH": "/usr/bin:/bin"]) throws -> (any RunningProcess, Collected) {
        let c = Collected()
        let q = DispatchQueue(label: "test")
        let p = try PosixLauncher().launch(
            CommandSpec(executable: "/bin/sh", arguments: ["-c", script], environment: env, lifeline: lifeline), queue: q,
            callbacks: ProcessCallbacks(stdoutLine: { c.out.append($0) }, stderrLine: { c.err.append($0) },
                                        exit: { c.status = $0; c.done.signal() }))
        return (p, c)
    }

    @Test func linesArriveInOrderAndTheExitComesLast() throws {
        let (_, c) = try start("for i in 1 2 3 4 5; do echo line$i; done; echo oops >&2; printf tail; exit 3")
        #expect(c.done.wait(timeout: .now() + 10) == .success)
        #expect(c.out == ["line1", "line2", "line3", "line4", "line5", "tail"] && c.err == ["oops"])
        #expect(c.status == .exited(3))
    }

    @Test func theEnvironmentIsExactlyTheOneGiven() throws {
        let (_, c) = try start("env | sort", env: ["PATH": "/usr/bin:/bin", "ONLY": "this"])
        #expect(c.done.wait(timeout: .now() + 10) == .success)
        let shellOwn = ["PWD", "OLDPWD", "SHLVL", "_"]
        #expect(c.out.filter { line in !shellOwn.contains { line.hasPrefix($0 + "=") } } == ["ONLY=this", "PATH=/usr/bin:/bin"])
    }

    @Test func theChildLeadsItsOwnProcessGroupAndInheritsNoOtherDescriptor() throws {
        // the shell's own descriptors, as lsof sees them (a builtin probe would see the copies bash saves at 10 and up)
        let script = "ps -o pgid= -p $$; /usr/sbin/lsof -a -p $$ -d 0-9999 -F f | sed -n 's/^f//p'"
        let (p, c) = try start(script, lifeline: true, env: ["PATH": "/usr/bin:/bin:/usr/sbin"])
        #expect(c.done.wait(timeout: .now() + 20) == .success)
        #expect(Int32(c.out.first?.trimmingCharacters(in: .whitespaces) ?? "") == p.pid)
        #expect(Array(c.out.dropFirst()) == ["0", "1", "2", "3"], "\(c.out.dropFirst())")  // stdio and the lifeline only
    }

    @Test func sigintStopsTheChildAndNothingOfItsGroupIsLeft() throws {
        let (p, c) = try start("echo started; sleep 30; echo not-reached")
        while c.out.isEmpty { usleep(20_000) }
        usleep(100_000)
        p.signalGroup(SIGINT)
        #expect(c.done.wait(timeout: .now() + 10) == .success)
        #expect(c.status == .signaled(SIGINT) || c.status == .exited(130), "\(String(describing: c.status))")
        #expect(!c.out.contains("not-reached"))
        usleep(100_000)
        #expect(killpg(p.pid, 0) == -1 && errno == ESRCH)
    }

    @Test func aGrandchildLeftBehindIsStoppedWhenTheChildExits() throws {
        // the child exits at once and leaves a sleeping grandchild in its group, holding stdout open
        let (p, c) = try start("sleep 30 & echo $!")
        #expect(c.done.wait(timeout: .now() + 15) == .success)
        let grandchild = Int32(c.out.first ?? "") ?? 0
        usleep(100_000)
        #expect(grandchild > 0 && kill(grandchild, 0) == -1 && errno == ESRCH)
        #expect(killpg(p.pid, 0) == -1)
    }

    @Test func theLifelineGivesNoEndOfFileWhileTheAppHoldsIt() throws {
        let t0 = Date()
        let (_, c) = try start("read -t 1 line <&3; echo read=$?", lifeline: true)
        #expect(c.done.wait(timeout: .now() + 10) == .success)
        #expect(Date().timeIntervalSince(t0) >= 0.9)  // the read waited for its timeout: the pipe stayed open
        #expect(c.out.first != "read=0")
    }

    @Test func signalsStartWithTheirDefaultAction() throws {
        // an app may run with SIGINT ignored; the child must not inherit that (trap -p lists inherited ignores)
        let (_, c) = try start("trap -p")
        #expect(c.done.wait(timeout: .now() + 10) == .success)
        #expect(c.out.isEmpty)
    }

    @Test func aMissingExecutableIsALaunchError() {
        #expect(throws: LaunchError.self) {
            _ = try PosixLauncher().launch(CommandSpec(executable: "/nonexistent/python", arguments: [], environment: [:]),
                                           queue: .global(), callbacks: ProcessCallbacks(stdoutLine: { _ in }, stderrLine: { _ in }, exit: { _ in }))
        }
    }

    @Test func capturedRunTimesOutAndStopsTheChild() {
        let t0 = Date()
        let out = CapturedRun.run(CommandSpec(executable: "/bin/sleep", arguments: ["30"], environment: [:]), timeout: 0.5)
        #expect(out != nil && out?.status == nil)
        #expect(Date().timeIntervalSince(t0) < 5)
        let ok = CapturedRun.run(CommandSpec(executable: "/bin/echo", arguments: ["hi"], environment: [:]), timeout: 5)
        #expect(ok?.stdout == "hi" && ok?.status == .exited(0))
    }
}
