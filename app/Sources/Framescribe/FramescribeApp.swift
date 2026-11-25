import AppKit
import FramescribeCore
import SwiftUI

@main
struct FramescribeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model = AppModel.shared

    var body: some Scene {
        Window("Framescribe", id: "main") {
            ContentView(model: model)
                .frame(minWidth: 640, minHeight: 480)
        }
        .defaultSize(width: 820, height: 620)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Open…") { model.chooseVideos() }
                    .keyboardShortcut("o", modifiers: .command)
            }
        }

        Settings {
            SettingsView(model: model)
        }
    }
}

/// Open-document events (a drop on the Dock or Finder icon, `open -a Framescribe.app video.mp4`), the window and
/// quitting. Closing the window while a video is processed keeps the app running with the job; a Dock icon click opens
/// the window again; only Quit asks (WindowPolicy).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var model: AppModel { AppModel.shared }

    func application(_ application: NSApplication, open urls: [URL]) {
        model.add(urls)
        if !NSApp.windows.contains(where: { $0.isVisible && $0.canBecomeMain }) { model.showMainWindow?() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        WindowPolicy.quitsWhenLastWindowCloses(hasRunningChild: model.hasRunningChild)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if WindowPolicy.reopensWindow(hasVisibleWindows: flag) { model.showMainWindow?() }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard WindowPolicy.onQuit(hasRunningChild: model.hasRunningChild) == .ask else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = model.queue.isBusy ? "A video is being processed." : "The speech model is being downloaded."
        alert.informativeText = model.queue.isBusy
            ? "Quitting stops it. Finished stages stay cached, so adding the video again continues from there."
            : "Quitting stops the download. Starting it again later resumes it."
        alert.addButton(withTitle: "Stop and Quit")
        alert.addButton(withTitle: "Cancel")
        alert.alertStyle = .warning
        guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        model.stopEverything {
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
