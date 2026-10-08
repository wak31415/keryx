import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The two states that replace every section: Keryx not found, and Keryx needing
    /// `keryx migrate` — each in the whole window, as the owner meets it — and the first
    /// with its Details open.
    /// Where the locator looked, on a Mac without Keryx.
    private static let notFound = LocateFailure(lookedAt: [
        "No Keryx folder has been chosen in the app.",
        "There is no ~/Library/LaunchAgents/dev.keryx.agent.plist, so the service is not installed.",
        "Your login shell (zsh) has no keryx on its PATH.",
    ])

    @MainActor static var onboardingEntries: [Entry] {
        [
            Entry("onboarding-locate", size: windowSize) {
                RootView().environment(AppModel.preview(location: .failure(notFound)))
            },
            Entry("onboarding-locate-details", size: CGSize(width: 760, height: 520)) {
                LocateView(detailsOpen: true).environment(AppModel.preview(location: .failure(notFound)))
            },
            Entry("onboarding-migration", size: windowSize) {
                RootView().environment(AppModel.preview(client: PreviewClient(
                    status: .success(PreviewData.statusMigration))))
            },
            Entry("onboarding-migration-moving", size: windowSize) {
                RootView().environment({
                    let model = AppModel.preview(client: PreviewClient(
                        status: .success(PreviewData.statusMigration),
                        migration: .holding([.step(step: "move", message: "Moving Keryx's files.")])))
                    model.migrate()
                    return model
                }())
            },
        ]
    }
}
