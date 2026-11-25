import Foundation

/// Numbers from the event stream as integers, without a trap: `Int(x)` stops the app when `x` is not finite or out of
/// range, and the stream is another program's output.
public enum Checked {
    /// `value` rounded toward zero, or nil when it is not finite or does not fit.
    public static func int(_ value: Double) -> Int? {
        guard value.isFinite else { return nil }
        return Int(exactly: value.rounded(.towardZero))
    }

    public static func int64(_ value: Double) -> Int64? {
        guard value.isFinite else { return nil }
        return Int64(exactly: value.rounded(.towardZero))
    }

    /// A share as a whole percentage, clamped to 0...100; 0 for NaN.
    public static func percent(_ fraction: Double) -> Int {
        int(min(1, max(0, fraction.isNaN ? 0 : fraction)) * 100) ?? 0
    }
}
