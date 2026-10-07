import KeryxKit
import SwiftUI

/// The Local Models screen. A placeholder until its own slice replaces this file's body.
public struct ModelsView: View {
    public init() {}

    public var body: some View {
        let item = SidebarItem.models
        ContentUnavailableView(item.title, systemImage: item.systemImage,
                               description: Text("Coming in this build."))
    }
}
