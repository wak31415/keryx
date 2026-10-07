import os

/// The app's own unified-log channel (`log show --predicate 'subsystem == "dev.keryx.app"'`);
/// the commands it runs are logged by KeryxKit's `LoggingRunner`, category `commands`.
enum Log {
    static let app = Logger(subsystem: "dev.keryx.app", category: "app")
}
