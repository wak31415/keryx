import KeryxKit
import SwiftUI

/// The question before the one write Keryx makes to Twilio (`phone webhooks --apply`):
/// both of the number's addresses, where they point now and where they would, and a yes.
/// It asks Twilio again as it opens and offers Point Here only on that answer — read for this
/// sheet, shown, and not failed — and only to a click: Return never applies it.
package struct PointHereSheet: View {
    /// What the Phone section showed, until this sheet's own answer is in.
    private let shown: PhoneWebhooks

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var applying = false
    @State private var error: KeryxError?
    /// Twilio's answer to the read this sheet made as it opened: the only one Point Here acts on.
    @State private var fresh: PhoneWebhooks?
    @State private var asking: Bool
    /// That read failed: what is shown may be stale, so nothing is offered.
    @State private var readError: KeryxError?

    /// `fresh`: the catalog's answer already in; otherwise the sheet asks for its own.
    package init(report: PhoneWebhooks, error: KeryxError? = nil, fresh: PhoneWebhooks? = nil) {
        shown = report
        _error = State(initialValue: error)
        _fresh = State(initialValue: fresh)
        _asking = State(initialValue: fresh == nil)
    }

    private var report: PhoneWebhooks { fresh ?? shown }

    private var canPointHere: Bool {
        PhoneWording.canPointHere(fresh: fresh, asking: asking, readFailed: readError != nil, applying: applying)
    }

    package var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                Text(PhoneWording.applyTitle(report))
                    .font(.headline)
                Text(PhoneWording.applyMessage)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding([.horizontal, .top], 20)

            Form {
                ForEach(PhoneWording.hooks(report)) { hook in
                    Section(hook.title) {
                        LabeledContent("Now") {
                            URLText(hook.now ?? "Not set", muted: hook.now == nil)
                        }
                        LabeledContent("New") {
                            URLText(hook.expected ?? "", muted: false)
                        }
                    }
                }
                if let error = error ?? readError {
                    Section {
                        Label {
                            Text(error.reported.map(KeryxError.sentence) ?? error.message)
                                .textSelection(.enabled)
                        } icon: {
                            Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .scrollDisabled(true)
            .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                if applying || asking {
                    ProgressView().controlSize(.small)
                    Text(applying ? "Changing the number…" : "Asking Twilio…").foregroundStyle(.secondary)
                } else if readError == nil, !PhoneWording.canApply(report) {
                    Text(report.matches ? "It points here already." : "Twilio couldn't be asked; try again later.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(applying)
                // A click, never Return: the yes is to both addresses, read just now.
                Button("Point Here", action: apply)
                    .disabled(!canPointHere)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 560)
        .task { await ask() }
    }

    /// Asks Twilio for this sheet; until it answers, and if it fails, nothing is offered.
    private func ask() async {
        asking = true
        readError = nil
        switch await model.freshPhoneWebhooks() {
        case let .success(answer): fresh = answer
        case let .failure(failure):
            fresh = nil
            readError = failure
        }
        asking = false
    }

    private func apply() {
        guard canPointHere else { return }
        applying = true
        error = nil
        Task {
            let answer = await model.applyPhoneWebhooks()
            applying = false
            switch answer {
            case .success: dismiss()
            case let .failure(failure): error = failure
            }
        }
    }
}

/// An address, whole and selectable.
private struct URLText: View {
    let text: String
    let muted: Bool

    init(_ text: String, muted: Bool) {
        self.text = text
        self.muted = muted
    }

    var body: some View {
        Text(text)
            .foregroundStyle(muted ? .secondary : .primary)
            .multilineTextAlignment(.trailing)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}
