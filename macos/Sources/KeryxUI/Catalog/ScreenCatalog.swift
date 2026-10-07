import KeryxKit
import SwiftUI

/// Every screen in every state worth seeing, by id: what `KeryxSnapshots` photographs and
/// the render tests draw.
///
/// An entry's `view` is called afresh for each render (light, then dark), so it builds
/// whatever it shows from nothing — a screen builds its own `AppModel` from `PreviewData`
/// there and hands it down with `.environment(...)`, so no two renders share state. Ids are
/// kebab-case, `<screen>-<state>` (`overview-idle`, `tasks-empty`), and become file names.
public struct ScreenCatalog {
    public struct Entry: Identifiable {
        public let id: String
        public let size: CGSize
        public let view: @MainActor () -> AnyView

        public init<V: View>(_ id: String, size: CGSize, @ViewBuilder view: @escaping @MainActor () -> V) {
            self.id = id
            self.size = size
            self.view = { AnyView(view()) }
        }
    }

    /// Every entry: the design kit's first, then each screen's, each list kept in its
    /// screen's own `Catalog/<Screen>+Catalog.swift`.
    @MainActor public static func entries() -> [Entry] {
        kitEntries + overviewEntries + sessionsEntries + modelsEntries + configurationEntries
            + menuEntries + onboardingEntries
    }

    /// A window's size in the catalog: what a full `RootView` is photographed at.
    static let windowSize = CGSize(width: 1100, height: 720)
}
