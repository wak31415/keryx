import Foundation
import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Phone section (Task UE) — connected and pointed elsewhere, pointed here, nothing
    /// saved yet, Twilio refusing — its account and Point Here sheets; the PIN sheet in each
    /// state `keryx pin` can be in; the secret sheet's key check; and the Overview's service
    /// rows with the service not installed.
    @MainActor static var phoneEntries: [Entry] {
        let screen = CGSize(width: 760, height: 1420)
        return [
            Entry("phone", size: screen) {
                PhoneView().environment(AppModel.preview(client: phoneClient()))
            },
            Entry("phone-matching", size: screen) {
                PhoneView().environment(AppModel.preview(client: phoneClient(
                    webhooks: PreviewData.phoneWebhooksMatching)))
            },
            Entry("phone-unset", size: CGSize(width: 760, height: 1320)) {
                PhoneView().environment(AppModel.preview(client: PreviewClient(
                    pin: .success(PreviewData.pinNone),
                    phoneNumbers: .success(PreviewData.phoneNumbersUnset),
                    phoneWebhooks: .success(PreviewData.phoneWebhooksUnset),
                    phoneTunnel: .success(PreviewData.phoneTunnelUnset))))
            },
            Entry("phone-twilio-refused", size: screen) {
                PhoneView().environment(AppModel.preview(client: phoneClient(
                    numbers: twilioRefused, webhooks: unasked, tunnel: unreachable)))
            },
            Entry("phone-account-sheet", size: CGSize(width: 500, height: 300)) {
                TwilioAccountSheet(savedSID: sampleSID, token: "preview-dots-only")
                    .environment(AppModel.preview(client: phoneClient()))
            },
            Entry("phone-account-sheet-verified", size: CGSize(width: 500, height: 320)) {
                TwilioAccountSheet(savedSID: sampleSID, token: "preview-dots-only",
                                   check: .works(detail: "Ada's account"))
                    .environment(AppModel.preview(client: phoneClient()))
            },
            Entry("phone-account-sheet-refused", size: CGSize(width: 500, height: 320)) {
                TwilioAccountSheet(savedSID: sampleSID, token: "preview-dots-only",
                                   check: .problem("Twilio didn't accept the account SID and auth token."))
                    .environment(AppModel.preview(client: phoneClient()))
            },
            Entry("phone-point-here-sheet", size: CGSize(width: 560, height: 418)) {
                PointHereSheet(report: PreviewData.phoneWebhooks)
                    .environment(AppModel.preview(client: phoneClient()))
            },
            Entry("phone-point-here-failed", size: CGSize(width: 560, height: 490)) {
                PointHereSheet(report: PreviewData.phoneWebhooks, error: .failed(
                    command: "keryx phone webhooks", exitCode: 1,
                    message: "The webhooks were not changed. Twilio didn't accept the account SID and auth token."))
                    .environment(AppModel.preview(client: phoneClient()))
            },
            Entry("phone-point-here-unasked", size: CGSize(width: 560, height: 418)) {
                PointHereSheet(report: unasked)
                    .environment(AppModel.preview(client: phoneClient(webhooks: unasked)))
            },
                        Entry("window-phone", size: windowSize) {
                RootView().environment(phoneWindowModel)
            },
        ] + pinEntries + secretCheckEntries + serviceEntries
    }

    // MARK: - The PIN sheet

    @MainActor private static var pinEntries: [Entry] {
        let size = CGSize(width: 480, height: 330)
        let small = CGSize(width: 480, height: 210)
        return [
            Entry("pin-sheet-none", size: size) {
                PinSheet(replaceConfirmed: false, first: "", second: "").environment(pinModel(PreviewData.pinNone))
            },
            Entry("pin-sheet-change", size: size) {
                PinSheet(replaceConfirmed: true, first: "", second: "").environment(pinModel(PreviewData.pinFile))
            },
            Entry("pin-sheet-set-unconfirmed", size: small) {
                PinSheet(replaceConfirmed: false, first: "", second: "").environment(pinModel(PreviewData.pinFile))
            },
            Entry("pin-sheet-locked", size: CGSize(width: 480, height: 400)) {
                PinSheet(replaceConfirmed: true, first: "", second: "").environment(pinModel(PreviewData.pinLocked))
            },
            Entry("pin-sheet-unusable", size: size) {
                PinSheet(replaceConfirmed: false, first: "", second: "").environment(pinModel(pin(source: "file", set: false)))
            },
            Entry("pin-sheet-environment", size: small) {
                PinSheet(replaceConfirmed: false, first: "", second: "").environment(pinModel(PreviewData.pinEnvironment))
            },
            Entry("pin-sheet-environment-malformed", size: CGSize(width: 480, height: 230)) {
                PinSheet(replaceConfirmed: false, first: "", second: "").environment(pinModel(pin(source: "environment", set: false)))
            },
            Entry("pin-sheet-jarvis", size: small) {
                PinSheet(replaceConfirmed: false, first: "", second: "").environment(pinModel(pin(source: "jarvis", set: true)))
            },
            Entry("pin-sheet-differ", size: size) {
                PinSheet(replaceConfirmed: false, first: "482915", second: "482916", submitted: true)
                    .environment(pinModel(PreviewData.pinNone))
            },
            Entry("pin-sheet-refused", size: CGSize(width: 480, height: 400)) {
                PinSheet(replaceConfirmed: false, first: "482915", second: "482915",
                         failure: PinWording.failure(.failed(command: "keryx pin set", exitCode: 1,
                                                             message: "A PIN is already set.")).text)
                    .environment(pinModel(PreviewData.pinNone))
            },
            Entry("pin-sheet-saved", size: CGSize(width: 480, height: 170)) {
                PinSheet(replaceConfirmed: true, first: "", second: "",
                         saved: PinSetResult(set: true, replaced: true, restartNeeded: true))
                    .environment(pinModel(PreviewData.pinFile))
            },
        ]
    }

    /// A model whose `keryx pin` says `status`.
    @MainActor private static func pinModel(_ status: PinStatus) -> AppModel {
        AppModel.preview(client: PreviewClient(pin: .success(status)))
    }

    /// The file's PIN status, moved: `source` and whether it counts as set.
    private static func pin(source: String, set: Bool) -> PinStatus {
        var status = PreviewData.pinFile
        status.source = source
        status.set = set
        return status
    }

    // MARK: - The secret sheet's check

    @MainActor private static var secretCheckEntries: [Entry] {
        let size = CGSize(width: 460, height: 232)
        return [
            Entry("configuration-secret-sheet-verified", size: size) {
                SecretSheet(key: "OPENAI_API_KEY", title: "OpenAI API key", replacing: true,
                            typed: "preview-dots-only", check: .works(detail: nil))
                    .environment(AppModel.preview())
            },
            Entry("configuration-secret-sheet-problem", size: size) {
                SecretSheet(key: "ANTHROPIC_API_KEY", title: "Anthropic API key", replacing: false,
                            typed: "preview-dots-only",
                            check: .problem("Anthropic didn't accept the key (401)."))
                    .environment(AppModel.preview())
            },
            Entry("configuration-secret-sheet-checking", size: CGSize(width: 460, height: 212)) {
                SecretSheet(key: "CODEX_API_KEY", title: "Codex API key", replacing: false,
                            typed: "preview-dots-only", check: .checking)
                    .environment(AppModel.preview())
            },
        ]
    }

    // MARK: - The Overview's service rows

    @MainActor private static var serviceEntries: [Entry] {
        [
            Entry("overview-service-not-installed", size: CGSize(width: 720, height: 900)) {
                OverviewView().environment(AppModel.preview(client: notInstalledClient))
            },
        ]
    }

    /// Keryx found, with no service installed and its tunnel not either.
    private static var notInstalledClient: PreviewClient {
        var status = PreviewData.status
        status.service.installed = false
        status.service.active = false
        status.health.reachable = false
        var service = PreviewData.service
        service.units.agent.installed = false
        service.units.agent.active = false
        service.units.tunnel.installed = false
        service.units.tunnel.active = false
        return PreviewClient(status: .success(status), service: .success(service))
    }

    // MARK: - Fixtures

    /// A made-up account SID, in the shape Twilio's are.
    static let sampleSID = "AC0123456789abcdef0123456789abcdef"

    /// The fixtures with a Twilio account saved: its SID and token, the number, and the
    /// owner's own (made-up) numbers.
    static var phoneConfig: [ConfigRow] {
        PreviewData.config.map { row in
            var row = row
            switch row.key {
            case "TWILIO_ACCOUNT_SID":
                row.value = .string(sampleSID)
                row.set = true
                row.source = "config.toml"
            case "TWILIO_AUTH_TOKEN":
                row.set = true
                row.source = "secrets.toml"
            case "TWILIO_NUMBER":
                row.value = .string("+15550001111")
                row.set = true
                row.source = "config.toml"
            case "ALLOWED_CALLERS":
                row.value = .array([.string("+15550109999")])
                row.set = true
                row.source = "config.toml"
            default:
                break
            }
            return row
        }
    }

    static func phoneClient(numbers: PhoneNumbers = PreviewData.phoneNumbers,
                            webhooks: PhoneWebhooks = PreviewData.phoneWebhooks,
                            tunnel: PhoneTunnel = PreviewData.phoneTunnel) -> PreviewClient {
        PreviewClient(config: .success(phoneConfig), phoneNumbers: .success(numbers),
                      phoneWebhooks: .success(webhooks), phoneTunnel: .success(tunnel))
    }

    /// Twilio refusing the saved pair: `phone numbers` says so, and nothing is listed.
    private static var twilioRefused: PhoneNumbers {
        var numbers = PreviewData.phoneNumbersUnset
        numbers.error = "Twilio didn't accept the account SID and auth token."
        return numbers
    }

    /// The webhooks with Twilio not asked successfully: where they should go, not where they do.
    private static var unasked: PhoneWebhooks {
        var webhooks = PreviewData.phoneWebhooks
        webhooks.current = nil
        return webhooks
    }

    /// The tunnel installed, and nothing answering at its host.
    private static var unreachable: PhoneTunnel {
        var tunnel = PreviewData.phoneTunnel
        tunnel.reachable = false
        return tunnel
    }

    @MainActor private static var phoneWindowModel: AppModel {
        let model = AppModel.preview(client: phoneClient())
        model.selection = .phone
        return model
    }
}
