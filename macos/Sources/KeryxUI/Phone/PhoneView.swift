import KeryxKit
import SwiftUI

/// The Phone section. A placeholder until its screen lands (Task UE): it reads from
/// `AppModel` only what the sidebar needs, so replacing this file is the whole change.
public struct PhoneView: View {
    public init() {}

    public var body: some View {
        ContentUnavailableView("Phone", systemImage: "phone",
                               description: Text("Your Twilio number, where its calls go, and the tunnel in front of this Mac."))
    }
}
