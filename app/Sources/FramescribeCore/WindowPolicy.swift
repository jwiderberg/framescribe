import Foundation

/// What closing the app's only window does. The behaviour of a single-window Mac utility that does work in the
/// background: closing the window while nothing runs quits the app; while a video is processed (or the speech model
/// downloads) the window closes and the app keeps running and working: clicking its Dock icon opens the window again,
/// with the live progress. Only an explicit Quit asks whether to stop the work. Closing never asks, so there is no
/// alert to cancel and no alert can come back.
public enum WindowPolicy {
    /// `applicationShouldTerminateAfterLastWindowClosed`
    public static func quitsWhenLastWindowCloses(hasRunningChild: Bool) -> Bool { !hasRunningChild }

    /// `applicationShouldHandleReopen` (a Dock icon click): open the window when none is visible.
    public static func reopensWindow(hasVisibleWindows: Bool) -> Bool { !hasVisibleWindows }

    public enum QuitAnswer: Equatable, Sendable {
        /// Nothing runs: quit at once.
        case now
        /// Ask; on "Stop and Quit" stop the child and quit once it has exited, on "Cancel" do nothing more.
        case ask
    }

    /// `applicationShouldTerminate`: only an explicit Quit reaches it while work runs (closing the window does not).
    public static func onQuit(hasRunningChild: Bool) -> QuitAnswer { hasRunningChild ? .ask : .now }
}
