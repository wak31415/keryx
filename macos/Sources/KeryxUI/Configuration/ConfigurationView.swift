import KeryxKit
import SwiftUI

/// The Configuration screen. A placeholder until its own slice replaces this file's body.
public struct ConfigurationView: View {
    public init() {}

    public var body: some View {
        let item = SidebarItem.configuration
        ContentUnavailableView(item.title, systemImage: item.systemImage,
                               description: Text("Coming in this build."))
    }
}
