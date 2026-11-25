import Foundation
import Testing
@testable import FramescribeCore

@Suite("Tool location and the child's environment")
struct EnvironmentTests {
    @Test func searchOrderIsLoginShellThenHomebrewThenTheAppsPathThenTheSystem() {
        let dirs = SearchPath.directories(loginShellPATH: "/home/user/bin:/opt/homebrew/bin:relative::/usr/bin/",
                                          processPATH: "/usr/bin:/bin:/usr/sbin:/sbin")
        #expect(dirs == ["/home/user/bin", "/opt/homebrew/bin", "/usr/bin", "/usr/local/bin", "/bin", "/usr/sbin", "/sbin"])
    }

    @Test func withoutALoginShellHomebrewIsStillSearched() {
        #expect(SearchPath.directories(loginShellPATH: nil, processPATH: "/usr/bin:/bin:/usr/sbin:/sbin")
            == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    }

    @Test func ffmpegIsFoundWhereHomebrewPutsIt() {
        let dirs = SearchPath.directories(loginShellPATH: nil, processPATH: "/usr/bin:/bin")
        let appleSilicon = ToolLocator(directories: dirs, probe: FakeProbe(executables: ["/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin/ffprobe"]))
        #expect(appleSilicon.locate("ffmpeg") == "/opt/homebrew/bin/ffmpeg" && appleSilicon.locate("ffprobe") == "/opt/homebrew/bin/ffprobe")
        let intel = ToolLocator(directories: dirs, probe: FakeProbe(executables: ["/usr/local/bin/ffmpeg"]))
        #expect(intel.locate("ffmpeg") == "/usr/local/bin/ffmpeg" && intel.locate("ffprobe") == nil)
    }

    @Test func theLoginShellsChoiceWinsOverHomebrew() {
        let dirs = SearchPath.directories(loginShellPATH: "/opt/local/bin:/usr/bin", processPATH: nil)
        let both = ToolLocator(directories: dirs, probe: FakeProbe(executables: ["/opt/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]))
        #expect(both.locate("ffmpeg") == "/opt/local/bin/ffmpeg")
        #expect(both.searchPATH.hasPrefix("/opt/local/bin:/usr/bin:/opt/homebrew/bin:/usr/local/bin"))
    }

    @Test func nothingFoundIsNil() {
        let none = ToolLocator(directories: ["/a", "/b"], probe: FakeProbe(executables: ["/a/ffmpeg-old", "/c/ffmpeg"]))
        #expect(none.locate("ffmpeg") == nil)
    }

    @Test func theRealProbeRejectsDirectoriesAndPlainFiles() {
        let dir = TempDir()
        let plain = dir.file("ffmpeg")
        #expect(!RealFileProbe().isExecutableFile(plain.path))
        #expect(!RealFileProbe().isExecutableFile(dir.url.path))
        #expect(RealFileProbe().isExecutableFile("/bin/sh"))
    }

    @Test func theLoginShellsEnvironmentIsReadBetweenTheMarkersOnly() {
        let output = """
        Welcome! (printed by a profile)
        PATH=/not/this/one

        __FRAMESCRIBE_ENV_BEGIN__
        PATH=/opt/homebrew/bin:/usr/bin:/bin
        HF_HOME=/Volumes/Big/hf
        MULTI=line one
        line two of MULTI
        EMPTY=
        __FRAMESCRIBE_ENV_END__
        PATH=/after
        """
        let env = LoginShell.parseEnvironment(output)
        #expect(env["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin")
        #expect(env["HF_HOME"] == "/Volumes/Big/hf" && env["MULTI"] == "line one" && env["EMPTY"] == "")
        #expect(env.count == 4)
        #expect(LoginShell.parseEnvironment("no markers at all").isEmpty)
    }

    @Test func theLoginShellCommandUsesTheUsersShellAsALoginShell() {
        let spec = LoginShell.command(shell: "/opt/homebrew/bin/fish", baseEnvironment: ["HOME": "/h"])
        #expect(spec.executable == "/opt/homebrew/bin/fish" && spec.arguments.prefix(2) == ["-l", "-c"])
        #expect(LoginShell.command(shell: nil, baseEnvironment: [:]).executable == "/bin/zsh")
        #expect(LoginShell.command(shell: "zsh", baseEnvironment: [:]).executable == "/bin/zsh")  // not an absolute path
    }

    @Test func readingARealLoginShellGivesAPATH() {
        let env = LoginShell.read(shell: "/bin/sh", baseEnvironment: ["HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"])
        #expect(env["PATH"]?.contains("/usr/bin") == true)
    }

    @Test func theChildEnvironmentIsBuiltFromScratch() {
        let app = ["HOME": "/home/user", "USER": "u", "TMPDIR": "/tmp/x", "PATH": "/usr/bin", "PYTHONPATH": "/evil",
                   "PYTHONHOME": "/evil", "DYLD_INSERT_LIBRARIES": "/evil.dylib", "HF_HOME": "/app/hf", "SECRET": "s"]
        let login = ["HF_HOME": "/login/hf", "HTTPS_PROXY": "http://proxy:3128", "PATH": "/ignored", "OTHER": "o"]
        let env = ChildEnvironment.make(processEnvironment: app, loginEnvironment: login, searchPATH: "/opt/homebrew/bin:/usr/bin")
        #expect(env["PATH"] == "/opt/homebrew/bin:/usr/bin")
        #expect(env["HOME"] == "/home/user" && env["USER"] == "u" && env["TMPDIR"] == "/tmp/x")
        #expect(env["HF_HOME"] == "/login/hf" && env["HTTPS_PROXY"] == "http://proxy:3128")
        #expect(env["PYTHONNOUSERSITE"] == "1" && env["PYTHONDONTWRITEBYTECODE"] == "1" && env["PYTHONSAFEPATH"] == "1")
        #expect(env["LANG"] == "en_US.UTF-8")
        for absent in ["PYTHONPATH", "PYTHONHOME", "DYLD_INSERT_LIBRARIES", "SECRET", "OTHER", "HF_HUB_OFFLINE"] {
            #expect(env[absent] == nil, "\(absent) must not reach the child")
        }
    }
}

@Suite("Paths and settings")
struct PathsTests {
    @Test func stemFollowsPythonsPathStem() {
        #expect(OutputLocation.stem(of: "talk.mp4") == "talk")
        #expect(OutputLocation.stem(of: "demo.2024.final.mov") == "demo.2024.final")
        #expect(OutputLocation.stem(of: "noextension") == "noextension")
        #expect(OutputLocation.stem(of: ".mp4") == ".mp4")
        #expect(OutputLocation.stem(of: "trailing.") == "trailing.")
        #expect(OutputLocation.stem(of: "a..mp4") == "a.")
    }

    @Test func theRunDirectoryIsTheOutputFolderPlusTheStem() {
        let dir = OutputLocation.runDirectory(video: URL(fileURLWithPath: "/Volumes/Rec/My talk.mov"),
                                              outputFolder: URL(fileURLWithPath: "/home/user/Documents/framescribe"))
        #expect(dir.path == "/home/user/Documents/framescribe/My talk")
        #expect(OutputLocation.report(in: dir).path == "/home/user/Documents/framescribe/My talk/report.md")
        #expect(OutputLocation.appLog(in: dir).lastPathComponent == "framescribe-app.log")
    }

    @Test func theOutputFolderSetting() {
        let home = URL(fileURLWithPath: "/home/user")
        #expect(OutputLocation.resolve(setting: nil, home: home).path == "/home/user/Documents/framescribe")
        #expect(OutputLocation.resolve(setting: "  ", home: home).path == "/home/user/Documents/framescribe")
        #expect(OutputLocation.resolve(setting: "relative/dir", home: home).path == "/home/user/Documents/framescribe")
        #expect(OutputLocation.resolve(setting: "/Volumes/Big/out/", home: home).path == "/Volumes/Big/out")
        #expect(OutputLocation.resolve(setting: "/a/b/../c", home: home).path == "/a/c")
        #expect(OutputLocation.resolve(setting: "~/Reports", home: home).path.hasSuffix("/Reports"))
    }

    final class MemoryStore: SettingsStore {
        var values: [String: Any] = [:]
        func string(forKey key: String) -> String? { values[key] as? String }
        func bool(forKey key: String) -> Bool { values[key] as? Bool ?? false }
        func set(_ value: Any?, forKey key: String) { values[key] = value }
        func removeObject(forKey key: String) { values[key] = nil }
    }

    @Test func settingsRoundTripThroughTheirStore() {
        let store = MemoryStore()  // the app uses UserDefaults.standard; the tests leave the user's preferences alone
        #expect(AppSettings.load(from: store) == AppSettings(outputFolder: nil))
        AppSettings(outputFolder: "/Volumes/Big/out").save(to: store)
        #expect(AppSettings.load(from: store) == AppSettings(outputFolder: "/Volumes/Big/out"))
        #expect(store.values["outputFolder"] as? String == "/Volumes/Big/out")
        AppSettings(outputFolder: nil).save(to: store)
        #expect(store.values["outputFolder"] == nil)
    }

    @Test func anOldAllowIncompleteSettingIsIgnored() {
        let store = MemoryStore()
        store.values = ["outputFolder": "/Volumes/Big/out", "allowIncomplete": true]  // stored by an earlier version
        let settings = AppSettings.load(from: store)
        #expect(settings == AppSettings(outputFolder: "/Volumes/Big/out") && AppSettings.retiredKeys == ["allowIncomplete"])
        let args = ToolCommands.runArguments(video: URL(fileURLWithPath: "/v/a.mp4"),
                                             outputFolder: settings.outputFolderURL(home: URL(fileURLWithPath: "/h")),
                                             runDirectory: URL(fileURLWithPath: "/Volumes/Big/out/a"))
        #expect(!args.contains("--allow-incomplete") && !args.contains("--strict-completeness"))
        settings.save(to: store)
        #expect(store.values["allowIncomplete"] as? Bool == true)  // left as it was: never read, never written
    }

    @Test func theRunFolderIsTheStemUnlessAnotherVideoOfThatNameHasRunThere() throws {
        let tmp = TempDir()
        let out = tmp.url.appendingPathComponent("out", isDirectory: true)
        let a = tmp.url.appendingPathComponent("a", isDirectory: true), b = tmp.url.appendingPathComponent("b", isDirectory: true)
        for d in [out, a, b] { try FileManager.default.createDirectory(at: d, withIntermediateDirectories: true) }
        let first = a.appendingPathComponent("talk.mp4"), second = b.appendingPathComponent("talk.mp4")
        for v in [first, second] { try "x".write(to: v, atomically: true, encoding: .utf8) }
        func recordRun(_ video: URL, in dir: URL) throws {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            // framescribe records the resolved path (on macOS /var/... is /private/var/...)
            let json = try JSONSerialization.data(withJSONObject: ["source": OutputLocation.canonicalPath(video.path), "status": "ok"])
            try json.write(to: dir.appendingPathComponent("run.json"))
        }
        // nothing there yet: <stem>
        #expect(OutputLocation.chooseRunDirectory(video: first, outputFolder: out).lastPathComponent == "talk")
        // a folder without a run.json (an earlier run that never started) is free
        try FileManager.default.createDirectory(at: out.appendingPathComponent("talk"), withIntermediateDirectories: true)
        #expect(OutputLocation.chooseRunDirectory(video: second, outputFolder: out).lastPathComponent == "talk")
        try recordRun(first, in: out.appendingPathComponent("talk"))
        // the same video again keeps its folder (and its cache); another one of the same name gets "talk 2"
        #expect(OutputLocation.chooseRunDirectory(video: first, outputFolder: out).lastPathComponent == "talk")
        #expect(OutputLocation.chooseRunDirectory(video: second, outputFolder: out).lastPathComponent == "talk 2")
        try recordRun(second, in: out.appendingPathComponent("talk 2"))
        #expect(OutputLocation.chooseRunDirectory(video: second, outputFolder: out).lastPathComponent == "talk 2")
        // a third video of that name: "talk 3"; an unreadable run.json counts as another video's
        let third = tmp.url.appendingPathComponent("talk.mp4")
        try "x".write(to: third, atomically: true, encoding: .utf8)
        #expect(OutputLocation.chooseRunDirectory(video: third, outputFolder: out).lastPathComponent == "talk 3")
        try FileManager.default.createDirectory(at: out.appendingPathComponent("talk 3"), withIntermediateDirectories: true)
        try "not json".write(to: out.appendingPathComponent("talk 3/run.json"), atomically: true, encoding: .utf8)
        #expect(OutputLocation.chooseRunDirectory(video: third, outputFolder: out).lastPathComponent == "talk 4")
        // a symbolic link to the first video is the first video
        let link = tmp.url.appendingPathComponent("link.mp4")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: first)
        let viaLink = OutputLocation.chooseRunDirectory(video: link, outputFolder: out, recordedSource: { dir in
            dir.lastPathComponent == "link" ? OutputLocation.canonicalPath(first.path) : nil
        })
        #expect(viaLink.lastPathComponent == "link")
    }

    @Test func whichFilesAreVideos() {
        let mp4 = URL(fileURLWithPath: "/v/a.MP4")
        #expect(VideoFiles.check(mp4, exists: true, isDirectory: false, conformsToMovie: false) == .video)
        for ext in ["mov", "m4v", "mkv", "webm"] {
            #expect(VideoFiles.hasVideoExtension(URL(fileURLWithPath: "/v/a.\(ext)")))
        }
        let pdf = URL(fileURLWithPath: "/v/a.pdf")
        #expect(VideoFiles.check(pdf, exists: true, isDirectory: false, conformsToMovie: false) == .notAVideo)
        let custom = URL(fileURLWithPath: "/v/a.mxf")  // a movie type the system knows
        #expect(VideoFiles.check(custom, exists: true, isDirectory: false, conformsToMovie: true) == .video)
        #expect(VideoFiles.check(mp4, exists: false, isDirectory: false, conformsToMovie: true) == .notAFile)
        #expect(VideoFiles.check(URL(fileURLWithPath: "/v/dir.mov"), exists: true, isDirectory: true, conformsToMovie: true) == .notAFile)
        #expect(VideoFiles.check(URL(string: "https://example.com/a.mp4")!, exists: true, isDirectory: false, conformsToMovie: true) == .notAFile)
    }
}

@Suite("Command lines")
struct CommandTests {
    let runtime = EmbeddedRuntime.ofApp(URL(fileURLWithPath: "/Apps/Framescribe.app"))
    let video = URL(fileURLWithPath: "/v/-starts with a dash.mp4")
    let out = URL(fileURLWithPath: "/home/user/Documents/framescribe")

    @Test func theRuntimeLivesInTheBundlesResources() {
        #expect(runtime.python.path == "/Apps/Framescribe.app/Contents/Resources/runtime/bin/python3.12")
        let probe = FakeProbe(executables: [runtime.python.path])
        #expect(EmbeddedRuntime.locate(bundleResources: URL(fileURLWithPath: "/Apps/Framescribe.app/Contents/Resources"), probe: probe) == runtime)
        #expect(EmbeddedRuntime.locate(bundleResources: URL(fileURLWithPath: "/elsewhere"), probe: probe) == nil)
        #expect(EmbeddedRuntime.locate(bundleResources: nil, probe: probe) == nil)
    }

    @Test func runArgumentsForEachSetting() {
        let dir = out.appendingPathComponent("-starts with a dash 2")
        #expect(ToolCommands.runArguments(video: video, outputFolder: out, runDirectory: dir)
            == ["run", "/v/-starts with a dash.mp4", "--out", "/home/user/Documents/framescribe",
                "--run-dir", "/home/user/Documents/framescribe/-starts with a dash 2", "--progress", "json"])
        // the output folder setting reaches --out, and nothing else changes
        let elsewhere = AppSettings(outputFolder: "/Volumes/Big")
        let folder = elsewhere.outputFolderURL(home: URL(fileURLWithPath: "/home/user"))
        #expect(ToolCommands.runArguments(video: video, outputFolder: folder, runDirectory: folder.appendingPathComponent("x"))
            == ["run", "/v/-starts with a dash.mp4", "--out", "/Volumes/Big", "--run-dir", "/Volumes/Big/x", "--progress", "json"])
        // the driver's test hook goes last; the app passes nothing there
        #expect(ToolCommands.runArguments(video: video, outputFolder: out, runDirectory: dir,
                                          extraArguments: ["--max-filled-share", "0.001"]).suffix(2) == ["--max-filled-share", "0.001"])
    }

    @Test func everyCommandRunsTheEmbeddedPythonWithTheEntryModule() {
        let c = ToolCommands(runtime: runtime, environment: ["PATH": "/usr/bin", "PYTHONNOUSERSITE": "1"])
        let run = c.run(video: video, outputFolder: out, runDirectory: out.appendingPathComponent("x"))
        #expect(run.executable == runtime.python.path)
        #expect(Array(run.arguments.prefix(3)) == ["-m", "framescribe_app_launcher", "run"])
        #expect(run.lifeline && run.environment["FRAMESCRIBE_APP_LIFELINE_FD"] == "3" && run.environment["PATH"] == "/usr/bin")
        #expect(c.doctor().arguments == ["-m", "framescribe_app_launcher", "doctor", "--json"])
        #expect(c.setup().arguments == ["-m", "framescribe_app_launcher", "setup", "--progress", "json"])
        #expect(c.version().arguments == ["-m", "framescribe_app_launcher", "--version"] && !c.version().lifeline)
        #expect(c.version().environment["FRAMESCRIBE_APP_LIFELINE_FD"] == nil)
    }
}
