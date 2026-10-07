import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// `AppModel`'s options and voice: the lists read once each, and every way of choosing how
/// the assistant speaks.
extension AppModelTests {
    // MARK: - Options

    @Test func optionsAreReadOnceUnlessForced() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.loadOptions("OPENAI_REALTIME_MODEL")
        await model.loadOptions("OPENAI_REALTIME_MODEL")
        #expect(client.calls.filter { $0 == "config options OPENAI_REALTIME_MODEL" }.count == 1)
        #expect(model.options["OPENAI_REALTIME_MODEL"]?.value == PreviewData.options["OPENAI_REALTIME_MODEL"])
        await model.loadOptions("OPENAI_REALTIME_MODEL", force: true)
        #expect(client.calls.filter { $0 == "config options OPENAI_REALTIME_MODEL" }.count == 2)
    }

    @Test func optionsThatFailAreStoredAndTriedAgain() async {
        let failure = KeryxError.failed(command: "keryx config options", exitCode: 2, message: "No such command 'options'")
        let client = StubClient { $0.options = { _ in .failure(failure) } }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.loadOptions("CODEX_MODEL")
        #expect(model.options["CODEX_MODEL"]?.error == failure)
        #expect(model.options["CODEX_MODEL"]?.value == nil)
        client.update { $0.options = { .success(PreviewData.builtinOptions($0)) } }
        await model.loadOptions("CODEX_MODEL")
        #expect(model.options["CODEX_MODEL"]?.value?.key == "CODEX_MODEL")
        #expect(model.options["CODEX_MODEL"]?.error == nil)
    }

    @Test func aFallbackListIsAValueNotAFailure() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.loadOptions("SUBAGENT_MODEL")
        let list = model.options["SUBAGENT_MODEL"]
        #expect(list?.error == nil)
        #expect(list?.value?.live == false)
        #expect(list?.value?.error != nil)
    }

    @Test func setByKeyWritesThroughTheSetPath() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await model.set(key: "LOCAL_AGENT_MODEL", to: "gpt-oss-20b") == nil)
        #expect(Array(client.calls.suffix(2)) == ["config set LOCAL_AGENT_MODEL gpt-oss-20b", "config list"])
        #expect(model.configNeedsRestart)
    }

    @Test func choosingAnEmptyOptionUnsetsTheSettingNeverSetsItEmpty() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        // "Codex's default", as `config options CODEX_MODEL` lists it.
        #expect(PreviewData.options["CODEX_MODEL"]?.options.first?.value == "")
        #expect(await model.choose("CODEX_MODEL", option: "") == nil)
        #expect(Array(client.calls.suffix(2)) == ["config unset CODEX_MODEL", "config list"])
        #expect(!client.calls.contains { $0.hasPrefix("config set CODEX_MODEL") })
        #expect(await model.choose("CODEX_MODEL", option: "gpt-6-sol") == nil)
        #expect(Array(client.calls.suffix(2)) == ["config set CODEX_MODEL gpt-6-sol", "config list"])
        #expect(model.configNeedsRestart)
    }

    @Test func relocatingForgetsTheOptions() async {
        let model = makeModel()
        await model.start()
        defer { model.stop() }
        await model.loadOptions("OPENAI_VOICE")
        #expect(model.options["OPENAI_VOICE"] != nil)
        await model.relocate()
        #expect(model.options.isEmpty)
    }

    // MARK: - Voice

    @Test func voiceIsADocument() async {
        let client = StubClient { $0.voice = .success(PreviewData.voiceServer) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        await model.refresh(.voice)
        #expect(client.calls.last == "voice")
        #expect(model.voice.value?.kind == .server)
        await model.refresh(.all)
        #expect(client.calls.filter { $0 == "voice" }.count == 2)
    }

    @Test func useOpenAISetsNeedsRestartAndReadsTheVoiceAgain() async {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        #expect(await model.useVoiceOpenAI() == nil)
        #expect(client.calls.contains("voice use openai"))
        #expect(client.calls.suffix(2).sorted() == ["config list", "voice"])
        #expect(model.configNeedsRestart)
    }

    @Test func aServerThatSavedSetsNeedsRestart() async throws {
        let client = StubClient()
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let answer = try await model.useVoiceServer(url: "http://studio.local:8000/v1", key: "vk", keepAnyway: false).get()
        #expect(answer.warnings.count == 1)
        #expect(client.calls.contains("voice use server http://studio.local:8000/v1 --key-stdin"))
        #expect(client.calls.suffix(2).sorted() == ["config list", "voice"])
        #expect(model.configNeedsRestart)
    }

    @Test func aServerThatFailedTheHandshakeSavesNothingAndNeedsNoRestart() async throws {
        let client = StubClient { $0.voiceServer = .success(PreviewClient.serverProblem) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let answer = try await model.useVoiceServer(url: "http://studio.local:8000/v1", key: nil, keepAnyway: false).get()
        #expect(answer.problem != nil)
        #expect(client.calls.last == "voice use server http://studio.local:8000/v1")
        #expect(!model.configNeedsRestart)
    }

    @Test func aRefusedServerIsAnError() async {
        let refusal = KeryxError.failed(command: "keryx voice use", exitCode: 2, message: "not a URL")
        let client = StubClient { $0.voiceServer = .failure(refusal) }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        let answer = await model.useVoiceServer(url: "x", key: nil, keepAnyway: true)
        #expect(answer == .failure(refusal))
        #expect(!model.configNeedsRestart)
    }

    @Test func aVoiceSetupThatWorksNeedsARestartAndReadsAgain() async {
        let client = StubClient {
            $0.voiceHere = { _ in
                VoiceSetupStream(AsyncThrowingStream {
                    $0.yield(.done(baseURL: "http://127.0.0.1:8765/v1"))
                    $0.finish()
                })
            }
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        model.setUpVoiceHere(voice: "bf_emma")
        #expect(await eventually { model.voice.value != nil && model.models.value != nil && model.config.value != nil })
        #expect(client.calls.contains("voice use here --voice bf_emma"))
        #expect(model.configNeedsRestart)
        #expect(model.voiceSetup.phase == .finished(baseURL: "http://127.0.0.1:8765/v1"))
    }

    @Test func aVoiceSetupThatFailsChangesNothing() async {
        let client = StubClient {
            $0.voiceHere = { _ in
                VoiceSetupStream(AsyncThrowingStream { $0.yield(.error("no room")); $0.finish() })
            }
        }
        let model = makeModel(client: client)
        await model.start()
        defer { model.stop() }
        model.setUpVoiceHere()
        #expect(await eventually { model.voiceSetup.phase == .failed("no room") })
        #expect(!model.configNeedsRestart)
        #expect(!client.calls.contains("voice"))
    }

    @Test func thePreviewModelHoldsTheVoiceAndEveryList() {
        let model = AppModel.preview()
        #expect(model.voice.value == PreviewData.voice)
        let keys = PreviewData.config.filter(\.hasOptions).map(\.key)
        #expect(keys.count == 11)
        #expect(Set(model.options.keys) == Set(keys))
        #expect(model.options["OPENAI_VOICE"]?.value == PreviewData.options["OPENAI_VOICE"])
        #expect(model.options["CODEX_MODEL"]?.value == PreviewData.options["CODEX_MODEL"])
        #expect(model.options["VOICE_SERVER_STT"]?.value == PreviewData.builtinOptions("VOICE_SERVER_STT"))
    }
}
