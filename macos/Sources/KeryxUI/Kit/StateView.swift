import KeryxKit
import SwiftUI

/// A screen's three states: its content once there is a value, a `ContentUnavailableView`
/// when loading failed with nothing to show, and a spinner until either — or while a retry
/// after a failure is under way, so Try Again visibly does something.
///
/// A value already shown stays shown through a later refresh or a later failure — the
/// screen does not blank while it reloads. Task 10's `Loadable` maps onto this with an
/// `init(_ loadable:retry:content:)` that passes `value`, `isLoading` and
/// `error.map { Failure(title: $0.title, message: $0.message) }`.
public struct StateView<Value, Content: View>: View {
    /// What went wrong, in words: a short title and a sentence.
    public struct Failure: Equatable, Sendable {
        public var title: String
        public var message: String

        public init(title: String, message: String) {
            self.title = title
            self.message = message
        }
    }

    private let value: Value?
    private let isLoading: Bool
    private let failure: Failure?
    private let retry: (() -> Void)?
    private let content: (Value) -> Content

    public init(value: Value?, isLoading: Bool, failure: Failure?, retry: (() -> Void)? = nil,
                @ViewBuilder content: @escaping (Value) -> Content) {
        self.value = value
        self.isLoading = isLoading
        self.failure = failure
        self.retry = retry
        self.content = content
    }

    public var body: some View {
        if let value {
            content(value)
        } else if let failure, !isLoading {
            ContentUnavailableView {
                Label(failure.title, systemImage: "exclamationmark.triangle")
            } description: {
                Text(failure.message)
                    .textSelection(.enabled)
            } actions: {
                if let retry {
                    Button("Try Again", action: retry)
                }
            }
        } else {
            ProgressView()
                .controlSize(.regular)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityLabel("Loading")
        }
    }
}

extension StateView {
    /// The screen for one of `AppModel`'s documents: its value, its error in words, and
    /// whether a load is under way.
    public init(_ loadable: Loadable<Value>, retry: (() -> Void)? = nil,
                @ViewBuilder content: @escaping (Value) -> Content) where Value: Sendable {
        self.init(value: loadable.value, isLoading: loadable.isLoading,
                  failure: loadable.error.map { Failure(title: $0.title, message: $0.message) },
                  retry: retry, content: content)
    }
}
