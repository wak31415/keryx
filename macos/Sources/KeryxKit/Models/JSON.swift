import Foundation

/// The one decoder every document model in `Models/` is decoded with: snake_case JSON keys
/// become the camelCase Swift properties the CLI's fields are named after, and a date is
/// ISO-8601 — `datetime.isoformat()` on the Python side — with or without fractional
/// seconds, and with any number of fractional digits (Python's microseconds, not just
/// `ISO8601DateFormatter`'s preferred three).
public enum JSON {
    public static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom(decodeDate)
        return decoder
    }()

    private static let formatters = ISOFormatters()

    private static func decodeDate(from decoder: Decoder) throws -> Date {
        let container = try decoder.singleValueContainer()
        let string = try container.decode(String.self)
        if let date = formatters.withFractionalSeconds.date(from: string)
            ?? formatters.plain.date(from: string) {
            return date
        }
        // `ISO8601DateFormatter` only parses a handful of fractional-digit counts; Python's
        // microsecond timestamps (six digits) are not always one of them, so fall back to
        // truncating to three (milliseconds) and trying again before giving up.
        if let truncated = truncatingFractionalSeconds(string),
           let date = formatters.withFractionalSeconds.date(from: truncated)
            ?? formatters.plain.date(from: truncated) {
            return date
        }
        throw DecodingError.dataCorruptedError(
            in: container, debugDescription: "Not an ISO-8601 date: \(string)")
    }

    private static func truncatingFractionalSeconds(_ string: String) -> String? {
        guard let dot = string.firstIndex(of: ".") else { return nil }
        var end = string.index(after: dot)
        while end < string.endIndex, string[end].isNumber {
            end = string.index(after: end)
        }
        let digits = string[string.index(after: dot)..<end]
        guard digits.count > 3 else { return nil }
        return string.replacingCharacters(in: string.index(after: dot)..<end, with: digits.prefix(3))
    }
}

/// `ISO8601DateFormatter` is not `Sendable` (it is mutable, in principle, after creation),
/// but nothing here ever mutates one after `init` — both formatters are configured once and
/// only ever read from, through `date(from:)`, which is the same accepted pattern as
/// `ProcessRunner`'s `Box`: the `@unchecked` conformance is asserted once, here, for a type
/// that is safe by construction rather than claimed for `ISO8601DateFormatter` itself.
private final class ISOFormatters: @unchecked Sendable {
    let withFractionalSeconds: ISO8601DateFormatter
    let plain: ISO8601DateFormatter

    init() {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        self.withFractionalSeconds = fractional

        let whole = ISO8601DateFormatter()
        whole.formatOptions = [.withInternetDateTime]
        self.plain = whole
    }
}
