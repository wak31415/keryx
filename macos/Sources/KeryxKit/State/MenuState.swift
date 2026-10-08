import Foundation

/// The window's sections, in the sidebar's order.
public enum SidebarItem: String, CaseIterable, Identifiable, Sendable {
    case overview, sessions, voice, models, phone, integrations, plugins, configuration

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .overview: "Overview"
        case .sessions: "Sessions"
        case .voice: "Voice"
        case .models: "Local Models"
        case .phone: "Phone"
        case .integrations: "Integrations"
        case .plugins: "Plugins"
        case .configuration: "Configuration"
        }
    }

    public var systemImage: String {
        switch self {
        case .overview: "gauge.with.dots.needle.33percent"
        case .sessions: "bubble.left.and.text.bubble.right"
        case .voice: "waveform"
        case .models: "cpu"
        case .phone: "phone"
        case .integrations: "link"
        case .plugins: "puzzlepiece.extension"
        case .configuration: "slider.horizontal.3"
        }
    }
}

/// What the menu bar says about Keryx, in one word: the first thing worth knowing.
public enum MenuState: Equatable, Sendable {
    case locating, notFound, needsMigration, stopped, notInstalled, idle
    /// This many tasks are running.
    case working(Int)
    /// Something to look at; the associated text says what.
    case attention(String)

    /// The attention when the status could not read Keryx's settings.
    public static let unreadableSettings = "Keryx can't read its settings"

    public var headline: String {
        switch self {
        case .locating: "Looking for Keryx…"
        case .notFound: "Keryx isn't on this Mac"
        case .needsMigration: "Keryx needs to move its files"
        case .stopped: "Keryx is stopped"
        case .notInstalled: "Keryx isn't running in the background yet"
        case .idle, .working: "Keryx is running"
        case let .attention(reason): reason
        }
    }

    public var symbol: MenuSymbol {
        switch self {
        case .locating, .idle, .working: .plain
        case .needsMigration, .attention: .dot
        case .notFound, .stopped, .notInstalled: .slashed
        }
    }
}

/// The menu's state from what the app knows, in order of what matters most.
///
/// A status that has not loaded yet reads as still looking; one that failed (other than for
/// a migration) is something to look at, titled by its error. A failure after a status did
/// load keeps the status it had.
public func menuState(location: Loadable<KeryxLocation>, status: StatusReport?,
                      statusError: KeryxError?) -> MenuState {
    if location.isLoading { return .locating }
    guard location.value != nil else {
        return location.error == nil ? .locating : .notFound
    }
    if case .migrationRequired = statusError { return .needsMigration }
    guard let status else {
        return statusError.map { .attention($0.title) } ?? .locating
    }
    if status.storage.migrationRequired { return .needsMigration }
    // Without its settings nothing else the status says can be trusted: not installed,
    // stopped and idle are all read through them.
    if !status.storage.ok { return .attention(MenuState.unreadableSettings) }
    if status.restart.state == "failed" { return .attention("Last restart failed") }
    if !status.service.installed && !status.health.reachable { return .notInstalled }
    if status.service.installed && !status.service.active && !status.health.reachable {
        return .stopped
    }
    let unreported = status.tasks.unreported
    if unreported > 0 {
        return .attention(unreported == 1 ? "1 result not yet told" : "\(unreported) results not yet told")
    }
    if status.tasks.running > 0 { return .working(status.tasks.running) }
    return .idle
}
