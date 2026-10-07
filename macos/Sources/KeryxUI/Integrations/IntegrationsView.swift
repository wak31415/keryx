import KeryxKit
import SwiftUI

/// The Integrations section. A placeholder until its screen lands (Task UD): it reads from
/// `AppModel` only what the sidebar needs, so replacing this file is the whole change.
public struct IntegrationsView: View {
    public init() {}

    public var body: some View {
        ContentUnavailableView("Integrations", systemImage: "link",
                               description: Text("The accounts Keryx connects to, and the sign-ins behind them."))
    }
}
