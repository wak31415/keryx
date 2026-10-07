import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The two states that replace every section: Keryx not found, and Keryx needing
    /// `keryx migrate` — each in the whole window, as the owner meets it.
    @MainActor static var onboardingEntries: [Entry] {
        [
            Entry("onboarding-locate", size: windowSize) {
                RootView().environment(AppModel.preview(location: .failure(LocateFailure(lookedAt: [
                    "No Keryx folder has been chosen in the app.",
                    "There is no ~/Library/LaunchAgents/dev.keryx.agent.plist, so the service is not installed.",
                    "Your login shell (zsh) has no keryx on its PATH.",
                ]))))
            },
            Entry("onboarding-migration", size: windowSize) {
                RootView().environment(AppModel.preview(client: PreviewClient(
                    status: .success(PreviewData.statusMigration))))
            },
        ]
    }
}
