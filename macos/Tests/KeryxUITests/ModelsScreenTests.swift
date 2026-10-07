import Foundation
import Testing
@testable import KeryxKit
@testable import KeryxUI

@MainActor @Suite struct ModelsScreenTests {
    let en = Locale(identifier: "en_US")

    func entry(_ name: String) -> CatalogModel {
        PreviewData.models.models.first { $0.name == name }!
    }

    @Test func catalogHasEveryModelsState() {
        let ids = Set(ScreenCatalog.entries().map(\.id))
        for id in ["models-populated", "models-downloading", "models-download-failed", "models-use-sheet",
                   "models-use-sheet-chosen", "models-empty-servers", "models-no-checkout", "models-error",
                   "window-models"] {
            #expect(ids.contains(id), "missing \(id)")
        }
    }

    @Test func recommendedFirstThenWhatFitsThenTheRest() {
        // As captured, the recommendation is also the largest model that fits.
        let names = ModelsOrder.sorted(PreviewData.models.models).map(\.name)
        #expect(names == ["qwen3-coder-30b-a3b", "qwen3.8-27b", "gpt-oss-20b", "gemma-4-12b",
                          "qwen3.5-9b", "qwen3.5-4b", "gpt-oss-120b", "qwen3.6-35b-a3b"])

        // A smaller recommendation still comes first, ahead of the larger models that fit.
        let smaller = PreviewData.models.models.map { model in
            var model = model
            model.recommended = model.name == "gemma-4-12b"
            return model
        }
        #expect(ModelsOrder.sorted(smaller).map(\.name) == [
            "gemma-4-12b", "qwen3-coder-30b-a3b", "qwen3.8-27b", "gpt-oss-20b",
            "qwen3.5-9b", "qwen3.5-4b", "gpt-oss-120b", "qwen3.6-35b-a3b"])
    }

    @Test func eachRowOffersOneThing() {
        #expect(ModelRow.action(for: entry("gemma-4-12b"), phase: nil, inUse: false) == .download)
        #expect(ModelRow.action(for: entry("gpt-oss-20b"), phase: nil, inUse: false) == .use)
        #expect(ModelRow.action(for: entry("qwen3-coder-30b-a3b"), phase: nil, inUse: true) == .inUse)
        #expect(ModelRow.action(for: entry("gpt-oss-120b"), phase: nil, inUse: false) == .tooBig)
    }

    @Test func aDownloadOutranksTheCatalog() {
        let gemma = entry("gemma-4-12b")
        #expect(ModelRow.action(for: gemma, phase: .running(done: 1, total: 2), inUse: false)
            == .downloading(done: 1, total: 2))
        #expect(ModelRow.action(for: gemma, phase: .failed("lost"), inUse: false) == .failed("lost"))
        // Finished before the list is read again: it is downloaded.
        #expect(ModelRow.action(for: gemma, phase: .finished, inUse: false) == .use)
    }

    @Test func progressReadsInTheTotalsUnit() {
        #expect(ModelRow.progress(done: 4_200_000_000, total: 7_100_000_000, locale: en) == "4.2 of 7.1 GB")
        #expect(ModelRow.progress(done: 300_000_000, total: 900_000_000, locale: en) == "300 of 900 MB")
        #expect(ModelRow.progress(done: 0, total: 0) == "Starting…")
    }

    @Test func sizesSayDownloadAndMemory() {
        let coder = entry("qwen3-coder-30b-a3b")
        #expect(ModelRow.sizes(coder) == "\(Formatting.gigabytes(18.6)) · needs 25 GB")
    }

    @Test func serverAddressesComeFromTheirURL() {
        #expect(Address.of("http://127.0.0.1:8090/v1", port: 1) == "127.0.0.1:8090")
        #expect(Address.of(nil, port: 8765) == "127.0.0.1:8765")
    }

    @Test func aDownloadingCatalogEntryShowsItsProgress() async throws {
        let model = AppModel.preview(client: PreviewClient(pull: .holding(done: 5, total: 10)))
        model.download("gemma-4-12b")
        for _ in 0..<50 where model.downloads.active["gemma-4-12b"] != .running(done: 5, total: 10) {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(model.downloads.active["gemma-4-12b"] == .running(done: 5, total: 10))
        model.downloads.cancel("gemma-4-12b")
        // Stopping, until the download has stopped: a spinner nobody can press again.
        #expect(model.downloads.active["gemma-4-12b"] == .cancelling)
        #expect(ModelRow.action(for: entry("gemma-4-12b"), phase: .cancelling, inUse: false) == .cancelling)
        for _ in 0..<50 where !model.downloads.active.isEmpty {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(model.downloads.active.isEmpty)
    }

    @Test func theChosenSheetSaysWhatIsLeftOnce() {
        #expect(UseModelSheet.detail(serving: false, llamaInstalled: true, checkout: true, serverInstalled: true,
                                     keryxRestart: true)
            == "Restart the model server to load it, then restart Keryx to start using it.")
        #expect(UseModelSheet.detail(serving: false, llamaInstalled: true, checkout: true, serverInstalled: false,
                                     keryxRestart: false) == "Start the model server to load it.")
        #expect(UseModelSheet.detail(serving: true, llamaInstalled: true, checkout: true, serverInstalled: true,
                                     keryxRestart: true) == "Restart Keryx to start using it.")
        #expect(UseModelSheet.detail(serving: false, llamaInstalled: false, checkout: true, serverInstalled: false,
                                     keryxRestart: true).hasSuffix("Then restart Keryx to start using it."))
    }
}
