import Foundation

/// One document the app shows, as it stands: the last value that loaded, the last error, when
/// it last loaded, and whether a load is under way.
///
/// A value survives a later failure (the screen does not blank because one refresh went
/// wrong); a success clears the error.
public struct Loadable<Value: Sendable>: Sendable {
    public var value: Value?
    public var error: KeryxError?
    public var updatedAt: Date?
    public var isLoading: Bool

    public init(value: Value? = nil, error: KeryxError? = nil, updatedAt: Date? = nil,
                isLoading: Bool = false) {
        self.value = value
        self.error = error
        self.updatedAt = updatedAt
        self.isLoading = isLoading
    }

    /// Nothing loaded, nothing loading.
    public static var idle: Self { Self() }

    public mutating func begin() {
        isLoading = true
    }

    public mutating func succeed(_ value: Value, at date: Date = Date()) {
        self.value = value
        error = nil
        updatedAt = date
        isLoading = false
    }

    public mutating func fail(_ error: KeryxError) {
        self.error = error
        isLoading = false
    }
}

extension Loadable: Equatable where Value: Equatable {}
