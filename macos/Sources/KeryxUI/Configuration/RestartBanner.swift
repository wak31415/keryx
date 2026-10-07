import KeryxKit
import SwiftUI

/// The row at the top of Configuration once a change is saved that the running service has
/// yet to load: the same restart, with the same two confirmations, as the Overview's.
struct RestartBanner: View {
    @Environment(AppModel.self) private var model
    @State private var confirming = false
    @State private var liveCalls: Int?
    @State private var failure: KeryxError?
    @State private var restarting = false

    var body: some View {
        Section {
            LabeledContent {
                HStack(spacing: 8) {
                    if restarting {
                        ProgressView().controlSize(.small)
                        Text("Restarting…").foregroundStyle(.secondary)
                    }
                    Button("Restart…") { confirming = true }
                        .disabled(restarting || model.status.value?.service.installed == false)
                }
            } label: {
                Label {
                    Text("Restart Keryx to apply your changes")
                } icon: {
                    Image(systemName: "arrow.clockwise").foregroundStyle(.orange)
                }
            }
        }
        .confirmationDialog("Restart Keryx?", isPresented: $confirming) {
            Button("Restart") { restart(force: false) }
        } message: {
            Text("Keryx restarts with your changes and comes back in a few seconds.")
        }
        .confirmationDialog(liveCallsTitle, isPresented: Binding(
            get: { liveCalls != nil }, set: { if !$0 { liveCalls = nil } }
        )) {
            Button("Restart Anyway", role: .destructive) { restart(force: true) }
        } message: {
            Text(liveCalls == 1 ? "Restarting ends it." : "Restarting ends them.")
        }
        .alert(failure?.title ?? "", isPresented: Binding(
            get: { failure != nil }, set: { if !$0 { failure = nil } }
        )) {
            Button("OK") {}
        } message: {
            Text(failure?.message ?? "")
        }
    }

    private var liveCallsTitle: String {
        liveCalls == 1 ? "1 call is in progress." : "\(liveCalls ?? 0) calls are in progress."
    }

    private func restart(force: Bool) {
        restarting = true
        Task {
            let error = await model.restart(force: force)
            restarting = false
            if case let .liveCalls(count) = error {
                liveCalls = count
            } else {
                failure = error
            }
        }
    }
}
