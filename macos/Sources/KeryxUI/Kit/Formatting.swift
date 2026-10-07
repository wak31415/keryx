import Foundation

/// The few ways the app writes a number or a date, in one place so every screen agrees.
public enum Formatting {
    /// A size the CLI reports in decimal gigabytes (`size_gb`, `ram_gb`: bytes / 1e9), as
    /// "36 GB" or "18.6 GB".
    public static func gigabytes(_ gigabytes: Double) -> String {
        bytes(Int64((gigabytes * 1e9).rounded()))
    }

    /// A byte count in decimal units ("1.2 GB", "2 TB"), matching what the CLI prints.
    public static func bytes(_ count: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: count, countStyle: .decimal)
    }

    /// Dollars to the cent: "$1.23". The agents' own estimates, so never more precise.
    public static func cost(_ dollars: Double, locale: Locale = .autoupdatingCurrent) -> String {
        dollars.formatted(.currency(code: "USD").precision(.fractionLength(2)).locale(locale))
    }

    /// How long ago, in words: "2 hours ago".
    public static func relative(_ date: Date, now: Date = .now, locale: Locale = .autoupdatingCurrent) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        formatter.locale = locale
        return formatter.localizedString(for: date, relativeTo: now)
    }
}
