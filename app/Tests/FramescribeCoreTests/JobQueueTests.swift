import Darwin
import Foundation
import Testing
@testable import FramescribeCore

@MainActor
@Suite("Queue")
struct JobQueueTests {
    let dir = TempDir()

    func videos(_ names: String...) -> [URL] { names.map { dir.file($0) } }

    func finishOK(_ p: FakeLauncher.Process) {
        p.say(Lines.runStart(Lines.everyStage), Lines.runEnd("ok"))
        p.exit(.exited(0))
    }

    @Test func jobsRunOneAtATimeInTheOrderTheyCame() {
        let (queue, launcher, _) = makeQueue(out: dir.url.appendingPathComponent("out"))
        queue.canStart = true
        let added = queue.enqueue(videos("a.mp4", "b.mov", "c.mkv")).added
        #expect(added.map(\.name) == ["a.mp4", "b.mov", "c.mkv"])
        #expect(launcher.launched.count == 1)
        #expect(added.map(\.state) == [.running, .waiting, .waiting])
        #expect(launcher.last.spec.arguments.contains(added[0].video.path))

        finishOK(launcher.last)
        #expect(added.map(\.state) == [.done, .running, .waiting] && launcher.launched.count == 2)
        #expect(launcher.last.spec.arguments.contains(added[1].video.path))
        launcher.last.say(Lines.runStart(Lines.everyStage), Lines.end("ingest", "failed", error: "boom"), Lines.runEnd("failed", error: "boom"))
        launcher.last.exit(.exited(2))
        #expect(added.map(\.state) == [.done, .failed, .running] && launcher.launched.count == 3)
        finishOK(launcher.last)
        #expect(added.map(\.state) == [.done, .failed, .done] && launcher.launched.count == 3 && !queue.isBusy)
    }

    @Test func aFileAddedWhileOneRunsWaitsForIt() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        queue.canStart = true
        queue.enqueue(videos("a.mp4"))
        let b = queue.enqueue(videos("b.mp4")).added[0]
        #expect(b.state == .waiting && launcher.launched.count == 1)
        finishOK(launcher.last)
        #expect(b.state == .running)
    }

    @Test func nothingStartsUntilTheChecksPass() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        let jobs = queue.enqueue(videos("a.mp4", "b.mp4")).added
        #expect(launcher.launched.isEmpty && jobs.allSatisfy { $0.state == .waiting })
        queue.canStart = true
        #expect(launcher.launched.count == 1 && jobs[0].state == .running)
    }

    @Test func nonVideosAndFoldersAreRejectedWithAReason() {
        let (queue, _, _) = makeQueue(out: dir.url)
        let notes = dir.file("notes.pdf")
        let folder = dir.url.appendingPathComponent("folder", isDirectory: true)
        try! FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let missing = dir.url.appendingPathComponent("gone.mp4")
        let result = queue.enqueue([notes, folder, missing] + videos("ok.webm"))
        #expect(result.added.map(\.name) == ["ok.webm"])
        #expect(result.rejected.map(\.reason) == ["notes.pdf is not a video file", "folder is not a file", "gone.mp4 is not a file"])
    }

    @Test func theSameFileIsNotQueuedTwiceWhileWaitingOrRunning() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        queue.canStart = true
        let v = videos("a.mp4", "b.mp4")
        queue.enqueue(v)
        #expect(queue.enqueue([v[0], v[1]]).added.isEmpty && queue.jobs.count == 2)
        finishOK(launcher.last)
        #expect(queue.enqueue([v[0]]).added.count == 1)  // finished: running it again is allowed (the cache is reused)
    }

    @Test func sameNameInAnotherFolderIsFlagged() {
        let (queue, _, _) = makeQueue(out: dir.url)
        let sub = dir.url.appendingPathComponent("sub", isDirectory: true)
        try! FileManager.default.createDirectory(at: sub, withIntermediateDirectories: true)
        let other = sub.appendingPathComponent("talk.mp4")
        try! "x".write(to: other, atomically: true, encoding: .utf8)
        let jobs = queue.enqueue(videos("talk.mp4") + [other]).added
        #expect(jobs[0].sharesOutputWith == other.path && jobs[1].sharesOutputWith == jobs[0].video.path)
        queue.remove(jobs[1])
        #expect(jobs[0].sharesOutputWith == nil)
    }

    @Test func stopSendsSIGINTThenSIGTERMThenSIGKILL() {
        let (queue, launcher, scheduler) = makeQueue(out: dir.url)
        queue.canStart = true
        let job = queue.enqueue(videos("a.mp4", "b.mp4")).added[0]
        let p = launcher.last
        queue.stop(job)
        #expect(p.signals == [SIGINT] && job.stopRequested)
        queue.stop(job)  // a second click sends nothing more
        #expect(p.signals == [SIGINT])
        #expect(scheduler.fire() == [20])
        #expect(p.signals == [SIGINT, SIGTERM])
        #expect(scheduler.fire() == [10])
        #expect(p.signals == [SIGINT, SIGTERM, SIGKILL])
        p.exit(.signaled(SIGKILL))
        #expect(job.state == .cancelled)
        #expect(queue.jobs[1].state == .running)  // the next one starts
    }

    @Test func aChildThatStopsInTimeIsNotEscalated() {
        let (queue, launcher, scheduler) = makeQueue(out: dir.url)
        queue.canStart = true
        let job = queue.enqueue(videos("a.mp4")).added[0]
        let p = launcher.last
        queue.stop(job)
        p.say(Lines.runStart(Lines.everyStage), Lines.runEnd("interrupted", error: "interrupted"))
        p.exit(.exited(130))
        scheduler.fire()
        #expect(p.signals == [SIGINT] && job.state == .cancelled)
    }

    @Test func stoppingAWaitingJobRemovesIt() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        queue.canStart = true
        let jobs = queue.enqueue(videos("a.mp4", "b.mp4", "c.mp4")).added
        queue.stop(jobs[1])
        #expect(queue.jobs.map(\.name) == ["a.mp4", "c.mp4"])
        queue.remove(jobs[0])  // running: not removed
        #expect(queue.jobs.count == 2 && launcher.last.signals.isEmpty)
        finishOK(launcher.last)
        #expect(launcher.last.spec.arguments.contains(jobs[2].video.path))
        queue.removeFinished()
        #expect(queue.jobs.map(\.name) == ["c.mp4"])
    }

    @Test func stopAllStopsTheRunningJobStartsNoOtherAndReportsWhenItHasExited() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        queue.canStart = true
        let jobs = queue.enqueue(videos("a.mp4", "b.mp4")).added
        var done = false
        queue.stopAll { done = true }
        #expect(!done && launcher.last.signals == [SIGINT] && !queue.canStart)
        launcher.last.exit(.exited(130))
        #expect(done && jobs[0].state == .cancelled && jobs[1].state == .waiting && launcher.launched.count == 1)
        var idle = false
        queue.stopAll { idle = true }
        #expect(idle)
    }

    @Test func aLaunchFailureFailsTheJobAndTheQueueGoesOn() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        launcher.failNextLaunch = "No such file or directory"
        queue.canStart = true
        let jobs = queue.enqueue(videos("a.mp4", "b.mp4")).added
        #expect(jobs[0].state == .failed && jobs[0].failure?.message.contains("No such file or directory") == true)
        #expect(jobs[1].state == .running)
    }

    @Test func thePlannerIsAskedWhenTheJobStartsSoSettingsChangedMeanwhileApply() {
        let (queue, launcher, _) = makeQueue(out: dir.url)
        final class Folder: @unchecked Sendable { var url = URL(fileURLWithPath: "/first") }
        let folder = Folder()
        let commands = ToolCommands(runtime: EmbeddedRuntime(root: URL(fileURLWithPath: "/r")), environment: [:])
        let out = dir.url.appendingPathComponent("chosen later")
        queue.planner = { job in
            JobLaunchPlan(command: commands.run(video: job.video, outputFolder: folder.url,
                                                runDirectory: OutputLocation.runDirectory(video: job.video, outputFolder: folder.url)),
                          runDirectory: OutputLocation.runDirectory(video: job.video, outputFolder: folder.url))
        }
        queue.enqueue(videos("a.mp4"))
        folder.url = out
        queue.canStart = true
        let args = launcher.last.spec.arguments
        #expect(args[args.firstIndex(of: "--out")! + 1] == out.path)
        #expect(!args.contains("--allow-incomplete") && !args.contains("--strict-completeness"))
    }

    @Test func twoVideosOfTheSameNameGetTwoFoldersAndTheJobKnowsWhichOne() throws {
        let out = dir.url.appendingPathComponent("out")
        let (queue, launcher, _) = makeQueue(out: out)
        let sub = dir.url.appendingPathComponent("sub", isDirectory: true)
        try FileManager.default.createDirectory(at: sub, withIntermediateDirectories: true)
        let other = sub.appendingPathComponent("talk.mp4")
        try "x".write(to: other, atomically: true, encoding: .utf8)
        queue.canStart = true
        let jobs = queue.enqueue(videos("talk.mp4") + [other]).added
        #expect(jobs[0].runDirectory?.lastPathComponent == "talk")
        // the first run records its source, as framescribe does in run.json
        let json = try JSONSerialization.data(withJSONObject: ["source": OutputLocation.canonicalPath(jobs[0].video.path)])
        try json.write(to: out.appendingPathComponent("talk/run.json"))
        finishOK(launcher.last)
        #expect(jobs[1].runDirectory?.lastPathComponent == "talk 2")
        let args = launcher.last.spec.arguments
        #expect(args[args.firstIndex(of: "--run-dir")! + 1] == out.appendingPathComponent("talk 2").path)
    }

    @Test func aVideoThatVanishedBeforeItsTurnFailsWithoutCreatingAFolder() throws {
        let out = dir.url.appendingPathComponent("out")
        let (queue, launcher, _) = makeQueue(out: out)
        queue.writesAppLog = true
        let v = videos("a.mp4", "gone.mp4")
        queue.canStart = true
        let jobs = queue.enqueue(v).added
        try FileManager.default.removeItem(at: v[1])
        finishOK(launcher.last)
        #expect(jobs[1].state == .failed && jobs[1].failure?.kind == .videoMissing && launcher.launched.count == 1)
        #expect(jobs[1].failure?.message.contains("no longer there") == true)
        #expect(!FileManager.default.fileExists(atPath: out.appendingPathComponent("gone").path))
    }

    @Test func theAppLogOfAVideoRunAgainKeepsTheEarlierRunAfterADatedSeparator() throws {
        let out = dir.url.appendingPathComponent("results")
        let (queue, launcher, _) = makeQueue(out: out)
        queue.writesAppLog = true
        queue.canStart = true
        let v = videos("talk.mp4")
        queue.enqueue(v)
        launcher.last.complain("first run")
        launcher.last.exit(.exited(1))
        queue.enqueue(v)
        launcher.last.complain("second run")
        launcher.last.exit(.exited(1))
        let log = try String(contentsOf: OutputLocation.appLog(in: out.appendingPathComponent("talk")), encoding: .utf8)
        #expect(log.contains("stderr: first run") && log.contains("stderr: second run"))
        let separators = log.components(separatedBy: "\n").filter { $0.hasPrefix("===== run started ") && $0.hasSuffix(" =====") }
        #expect(separators.count == 2)
        let second = log.range(of: separators[1], options: .backwards)!.lowerBound
        #expect(log.range(of: "first run")!.lowerBound < second && second < log.range(of: "second run")!.lowerBound)
    }

    @Test func theRunDirectoryIsCreatedAndTheAppLogWritten() throws {
        let out = dir.url.appendingPathComponent("results")
        let (queue, launcher, _) = makeQueue(out: out)
        queue.writesAppLog = true
        queue.canStart = true
        let job = queue.enqueue(videos("talk.mp4")).added[0]
        let p = launcher.last
        p.say(Lines.runStart(Lines.everyStage))
        p.complain("a warning on stderr")
        p.exit(.exited(1))
        let log = try String(contentsOf: OutputLocation.appLog(in: out.appendingPathComponent("talk")), encoding: .utf8)
        #expect(log.contains("command: /App.app/Contents/Resources/runtime/bin/python3.12 -m framescribe_app_launcher run"))
        #expect(log.contains("\"event\": \"run_start\"") && log.contains("stderr: a warning on stderr") && log.contains("exit: exit status 1"))
        #expect(job.appLog?.lastPathComponent == "framescribe-app.log")
    }
}
