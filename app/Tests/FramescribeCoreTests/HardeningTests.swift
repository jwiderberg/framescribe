import Darwin
import Foundation
import Testing
@testable import FramescribeCore

/// The security review's findings on the app's side: run folder names that escape the output folder, writes through a
/// planted symbolic link, the Hugging Face token in the child's environment, numbers from the event stream that trap.
@Suite("Hardening")
struct HardeningTests {
    let dir = TempDir()

    @Test func theRunFolderNameIsTheStemUnlessThatIsUnsafe() {
        #expect(OutputLocation.runFolderName(of: "talk.mov") == "talk")
        #expect(OutputLocation.runFolderName(of: "a.b.mp4") == "a.b")
        #expect(OutputLocation.runFolderName(of: ".mp4") == ".mp4")
        for unsafe in ["...mov", "..mov", "a\nb.mov", "a\tb.mov", "\u{1B}[31mred.mov", "a\u{85}b.mov", "a\u{2028}b.mov",
                       "\u{0}x.mov"] {
            #expect(OutputLocation.runFolderName(of: unsafe) == OutputLocation.safeRunName, "\(unsafe.debugDescription)")
        }
        // the same rule as framescribe's: "...mov" has the stem "..", the parent of the output folder
        #expect(OutputLocation.stem(of: "...mov") == ".." && OutputLocation.stem(of: "..mov") == ".")
    }

    @Test func aVideoCalledDotDotDotMovRunsInsideTheOutputFolder() {
        let out = URL(fileURLWithPath: "/home/user/Documents/framescribe")
        for name in ["...mov", "..mov", "x\ny.mov"] {
            let run = OutputLocation.runDirectory(video: URL(fileURLWithPath: "/v/\(name)"), outputFolder: out)
            #expect(run.path == "/home/user/Documents/framescribe/video")
            let chosen = OutputLocation.chooseRunDirectory(video: URL(fileURLWithPath: "/v/\(name)"), outputFolder: out,
                                                           recordedSource: { _ in nil })
            #expect(chosen.deletingLastPathComponent().path == out.path && chosen.lastPathComponent == "video")
        }
    }

    @Test func aRunFolderThatIsASymbolicLinkIsSkipped() throws {
        let out = dir.url.appendingPathComponent("out", isDirectory: true)
        let elsewhere = dir.url.appendingPathComponent("elsewhere", isDirectory: true)
        for d in [out, elsewhere] { try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true) }
        try FileManager.default.createSymbolicLink(at: out.appendingPathComponent("talk"), withDestinationURL: elsewhere)
        let video = dir.file("talk.mp4")
        let chosen = OutputLocation.chooseRunDirectory(video: video, outputFolder: out)
        #expect(chosen.lastPathComponent == "talk 2")
        #expect(!OutputLocation.isRunDirectory(out.appendingPathComponent("talk"), in: out))
        #expect(OutputLocation.isRunDirectory(chosen, in: out))
    }

    @MainActor
    @Test func theQueueRefusesAnOutputFolderThatIsASymbolicLink() throws {
        let out = dir.url.appendingPathComponent("results", isDirectory: true)
        let elsewhere = dir.url.appendingPathComponent("elsewhere", isDirectory: true)
        for d in [out, elsewhere] { try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true) }
        let link = out.appendingPathComponent("talk")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: elsewhere)
        let (queue, launcher, _) = makeQueue(out: out)
        let commands = ToolCommands(runtime: EmbeddedRuntime(root: URL(fileURLWithPath: "/App.app/Contents/Resources/runtime")),
                                    environment: ["PATH": "/usr/bin"])
        queue.planner = { job in  // a planner that hands over the link (the app's own skips it)
            JobLaunchPlan(command: commands.run(video: job.video, outputFolder: out, runDirectory: link), runDirectory: link)
        }
        queue.writesAppLog = true
        queue.canStart = true
        let job = queue.enqueue([dir.file("talk.mp4")]).added[0]
        #expect(job.state == .failed && job.failure?.message.contains("symbolic link") == true)
        #expect(launcher.launched.isEmpty)
        #expect(try FileManager.default.contentsOfDirectory(atPath: elsewhere.path).isEmpty)
    }

    @MainActor
    @Test func theAppLogIsNeverWrittenThroughAPlantedLink() throws {
        let victim = dir.file("victim.txt", contents: "ORIGINAL\n")
        let run = dir.url.appendingPathComponent("run", isDirectory: true)
        try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
        let log = OutputLocation.appLog(in: run)
        try FileManager.default.createSymbolicLink(at: log, withDestinationURL: victim)
        let writer = JobLogWriter(url: log, command: CommandSpec(executable: "/bin/true", arguments: [], environment: [:]))
        #expect(writer == nil)
        #expect(try String(contentsOf: victim, encoding: .utf8) == "ORIGINAL\n")
        // a real file is appended to, as before
        try FileManager.default.removeItem(at: log)
        let fresh = JobLogWriter(url: log, command: CommandSpec(executable: "/bin/true", arguments: [], environment: [:]))
        fresh?.write("a line")
        fresh?.close()
        #expect(try String(contentsOf: log, encoding: .utf8).contains("a line"))
    }

    @Test func theChildDoesNotGetTheHuggingFaceToken() {
        let env = ChildEnvironment.make(processEnvironment: ["HOME": "/h", "HF_TOKEN": "hf_app"],
                                        loginEnvironment: ["HF_TOKEN": "hf_login", "HF_ENDPOINT": "https://mirror.example",
                                                           "HF_HOME": "/hf"],
                                        searchPATH: "/usr/bin")
        #expect(env["HF_TOKEN"] == nil)
        #expect(env["HF_ENDPOINT"] == "https://mirror.example" && env["HF_HOME"] == "/hf")
        #expect(!ChildEnvironment.fromLoginShell.contains("HF_TOKEN"))
    }

    @Test func absurdNumbersInTheEventStreamDoNotTrap() {
        func decode(_ line: String) -> Result<ToolEvent, EventDecodeError> { EventDecoder.decode(line) }
        let huge = decode(#"{"event": "run_end", "t": 1, "status": "ok", "completeness": {"summary": "s", "filled_words": 1e300, "primary_words": -1e300}}"#)
        guard case let .success(.runEnd(e)) = huge else { Issue.record("run_end did not decode"); return }
        #expect(e.completeness?.filledWords == nil && e.completeness?.primaryWords == nil)
        let progress = decode(#"{"event": "setup_progress", "t": 1, "file": "model.safetensors", "done": 1e300, "total": 10}"#)
        guard case .failure = progress else { Issue.record("an impossible byte count was accepted"); return }
        #expect(Clock.format(1e300) == "0:00" && Clock.format(-5) == "0:00" && Clock.format(.nan) == "0:00")
        #expect(Clock.format(3723) == "1:02:03")
        #expect(Checked.percent(.nan) == 0 && Checked.percent(.infinity) == 100 && Checked.percent(0.5) == 50)
        #expect(Checked.int(1e300) == nil && Checked.int(-0.9) == 0 && Checked.int64(2.5e9) == 2_500_000_000)
        var p = SetupProgress()
        p.sizeGB = 1e300
        #expect(p.totalBytes == 0)
    }
}
