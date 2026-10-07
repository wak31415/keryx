import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Local Models screen: a Mac with a model in use and others to download, a
    /// download under way and one that failed, the Use This Model sheet before and after,
    /// a Mac with no local agent yet, a Keryx with no checkout, a list that failed to load,
    /// and the whole window around it.
    @MainActor static var modelsEntries: [Entry] {
        let screen = CGSize(width: 720, height: 900)
        return [
            Entry("models-populated", size: screen) {
                ModelsView().environment(AppModel.preview())
            },
            Entry("models-downloading", size: CGSize(width: 720, height: 760)) {
                ModelsView().environment(downloading(.holding(done: 4_200_000_000, total: 7_100_000_000)))
            },
            Entry("models-stopping", size: CGSize(width: 720, height: 760)) {
                ModelsView().environment(stopping)
            },
            Entry("models-stale-server", size: CGSize(width: 720, height: 560)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(models: .success(staleServer))))
            },
            Entry("models-download-failed", size: CGSize(width: 720, height: 760)) {
                ModelsView().environment(downloading(.events([
                    .progress(done: 1_300_000_000, total: 7_100_000_000),
                    .error("The connection to huggingface.co was lost. Try again to pick up where it stopped."),
                ])))
            },
            Entry("models-use-sheet", size: CGSize(width: 440, height: 176)) {
                UseModelSheet(entry: gptOss20b).environment(AppModel.preview())
            },
            Entry("models-use-sheet-chosen", size: CGSize(width: 440, height: 176)) {
                UseModelSheet(entry: gptOss20b, stage: .chosen).environment(chosenModel)
            },
            Entry("models-empty-servers", size: CGSize(width: 720, height: 700)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(
                    models: .success(PreviewData.modelsEmptyServers))))
            },
            Entry("models-no-checkout", size: CGSize(width: 720, height: 560)) {
                ModelsView().environment(AppModel.preview(location: .success(noCheckout)))
            },
            Entry("models-error", size: CGSize(width: 720, height: 420)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(
                    models: .failure(.timedOut(command: "keryx models list --json")))))
            },
            Entry("window-models", size: windowSize) {
                RootView().environment(onModels)
            },
        ]
    }

    /// A model downloading Gemma 4 12B, the recommended model, with its pull doing `pull`.
    @MainActor private static func downloading(_ pull: PreviewClient.Pull) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(pull: pull))
        model.download("gemma-4-12b")
        return model
    }

    /// A download asked to stop, still stopping.
    @MainActor private static var stopping: AppModel {
        let model = downloading(.slowToStop(done: 4_200_000_000, total: 7_100_000_000))
        // The first progress event lands on the next turn of the main actor; until then
        // there is nothing running to cancel.
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        model.downloads.cancel("gemma-4-12b")
        return model
    }

    /// gpt-oss 20B chosen, and the model server still running Qwen3-Coder: the restart that
    /// loads it is still to come.
    private static var staleServer: ModelsReport {
        var report = PreviewData.models
        report.servers.localAgent.model = "gpt-oss-20b"
        report.servers.llm.model = "gpt-oss-20b"
        report.servers.llm.servedModel = "qwen3-coder-30b-a3b"
        return report
    }

    /// Downloaded and not in use: what Use This Model… is offered on.
    private static var gptOss20b: CatalogModel {
        PreviewData.models.models.first { $0.name == "gpt-oss-20b" }!
    }

    /// Just chosen, with the restart it asks for still to come.
    @MainActor private static var chosenModel: AppModel {
        let model = AppModel.preview()
        model.configNeedsRestart = true
        return model
    }

    /// Keryx installed on its own (`uv tool install`), with no checkout to install the model
    /// server's service from.
    private static var noCheckout: KeryxLocation {
        var location = KeryxLocation.fixture
        location.checkout = nil
        location.executable = "/Users/you/.local/bin/keryx"
        location.prefixArguments = []
        location.workingDirectory = "/Users/you"
        return location
    }

    /// The whole window, on Local Models.
    @MainActor private static var onModels: AppModel {
        let model = AppModel.preview()
        model.selection = .models
        return model
    }
}
