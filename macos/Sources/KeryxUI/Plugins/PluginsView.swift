import KeryxKit
import SwiftUI

/// The Plugins section. A placeholder until its screen lands (Task UD): it reads from
/// `AppModel` only what the sidebar needs, so replacing this file is the whole change.
public struct PluginsView: View {
    public init() {}

    public var body: some View {
        ContentUnavailableView("Plugins", systemImage: "puzzlepiece.extension",
                               description: Text("The optional voice tools, and your own."))
    }
}
