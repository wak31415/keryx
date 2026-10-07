import KeryxKit
import SwiftUI

/// The Sessions screen. A placeholder until its own slice replaces this file's body.
public struct SessionsView: View {
    public init() {}

    public var body: some View {
        let item = SidebarItem.sessions
        ContentUnavailableView(item.title, systemImage: item.systemImage,
                               description: Text("Coming in this build."))
    }
}
