import KeryxKit
import SwiftUI

/// The question before the one write Keryx makes to Twilio (`phone webhooks --apply`):
/// both of the number's addresses, where they point now and where they would, and a yes.
package struct PointHereSheet: View {
    let report: PhoneWebhooks

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var applying = false
    @State private var error: KeryxError?

    package init(report: PhoneWebhooks, error: KeryxError? = nil) {
        self.report = report
        _error = State(initialValue: error)
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
                if let error {
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
                if applying {
                    ProgressView().controlSize(.small)
                    Text("Changing the number…").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(applying)
                Button("Point Here", action: apply)
                    .keyboardShortcut(.defaultAction)
                    .disabled(applying || !PhoneWording.canApply(report))
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 560)
    }

    private func apply() {
        guard !applying else { return }
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
