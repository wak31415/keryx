import KeryxKit
import SwiftUI

/// The Setup Assistant: the steps to a first working call, from `AppModel.setupPlan`, in
/// the window with id `KeryxWindow.setup`. A placeholder until its screen lands (Task UC).
public struct SetupAssistantView: View {
    @Environment(AppModel.self) private var model

    public init() {}

    public var body: some View {
        ContentUnavailableView("Set Up Keryx", systemImage: "checklist",
                               description: Text("The steps to a first call, one at a time."))
            .task { await model.refresh(.setupPlan) }
    }
}
