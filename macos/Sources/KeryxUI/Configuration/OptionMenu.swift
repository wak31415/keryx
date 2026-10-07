import KeryxKit
import SwiftUI

/// A setting's options as a pop-up menu: what `keryx config options` lists, each by its
/// label; the value chosen now, even when the list lacks it; and Other… for a value of one's
/// own, when the list allows one or could not be read. The list is read the first time the
/// menu is shown (`AppModel.loadOptions`), with a spinner beside it until it is.
///
/// It writes nothing itself: `choose` is handed the value picked or typed — empty for an
/// option that means the setting unset, which `AppModel.choose` unsets.
struct OptionMenu: View {
    let key: String
    /// The setting's name for a person, which labels the menu and the Other… sheet.
    let title: String
    /// The value chosen now; empty for none.
    let current: String
    let choose: (String) -> Void

    @Environment(AppModel.self) private var model
    @State private var typing = false

    var body: some View {
        let loadable = model.options[key]
        HStack(spacing: 8) {
            if loadable?.value == nil, loadable?.error == nil {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Loading \(title)")
            }
            Picker(title, selection: selection) {
                ForEach(OptionWording.items(loadable?.value, current: current)) { item in
                    Text(item.label).tag(item.value)
                }
                if OptionWording.offersOther(loadable) {
                    Divider()
                    Text(OptionWording.otherTitle).tag(OptionWording.other)
                }
            }
            .pickerStyle(.menu)
            .labelsHidden()
            .fixedSize()
        }
        .task { await model.loadOptions(key) }
        .sheet(isPresented: $typing) {
            OtherValueSheet(title: title, current: current, choose: choose)
        }
    }

    private var selection: Binding<String> {
        Binding {
            current
        } set: { picked in
            if picked == OptionWording.other {
                typing = true
            } else if picked != current {
                // Empty is an option too ("Codex's default"): the setting unset.
                choose(picked)
            }
        }
    }
}

/// A value of one's own for a setting whose list lacks it: typed, then handed back.
package struct OtherValueSheet: View {
    let title: String
    let current: String
    let choose: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var text: String

    package init(title: String, current: String, choose: @escaping (String) -> Void) {
        self.title = title
        self.current = current
        self.choose = choose
        _text = State(initialValue: current)
    }

    package var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Other " + Wording.titleCase(title))
                .font(.headline)
                .padding([.horizontal, .top], 20)

            Form {
                Section {
                    TextField(title, text: $text, prompt: Text("Exact name"))
                        .onSubmit(use)
                } footer: {
                    Text("For one that isn't in the list. Type its name exactly as the provider spells it.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .scrollDisabled(true)
            .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Use", action: use)
                    .keyboardShortcut(.defaultAction)
                    .disabled(trimmed.isEmpty || trimmed == current)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 420)
    }

    private var trimmed: String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func use() {
        guard !trimmed.isEmpty, trimmed != current else { return }
        choose(trimmed)
        dismiss()
    }
}

/// A setting's menu as a row of its own — the title, the menu, a spinner while a choice is
/// saved, and what went wrong — for a screen other than Configuration (Voice, Local Models).
/// A choice is written through `AppModel.choose`, and `written` runs once it is.
struct SettingMenuRow: View {
    let key: String
    let title: String
    /// A line under the title.
    var subtitle: String?
    let current: String
    var written: () async -> Void = {}

    @Environment(AppModel.self) private var model
    @State private var writing = false
    @State private var pending: String?
    @State private var error: KeryxError?

    var body: some View {
        LabeledContent {
            VStack(alignment: .trailing, spacing: 4) {
                HStack(spacing: 8) {
                    if writing {
                        ProgressView().controlSize(.small)
                    }
                    OptionMenu(key: key, title: title, current: pending ?? current) { value in
                        save(value)
                    }
                    .disabled(writing)
                }
                if let detail = OptionWording.detail(of: pending ?? current, in: model.options[key]?.value) {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } label: {
            Text(title)
            if let subtitle {
                Text(subtitle)
            }
            if let note = OptionWording.note(model.options[key], titles: Wording.titles(model.config.value)) {
                Text(note)
                    .lineLimit(2)
                    .help(note)
            }
            if let error {
                Text(ConfigWording.errorText(error.reported ?? error.message, key: key, title: title))
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    private func save(_ value: String) {
        guard !writing else { return }
        writing = true
        pending = value
        error = nil
        Task {
            let failure = await model.choose(key, option: value)
            if failure == nil { await written() }
            error = failure
            pending = nil
            writing = false
        }
    }
}

/// How an option menu names what it offers.
enum OptionWording {
    /// One item in the menu.
    struct Item: Identifiable, Equatable {
        let value: String
        let label: String
        var id: String { value }
    }

    /// The tag of Other…, which no real value can be.
    static let other = "\u{0}other"
    static let otherTitle = "Other…"
    /// The item for no value, when the list has none of its own ("Codex's default").
    static let notSet = "Not set"

    /// The menu's items: the list's options by their labels, with the value chosen now
    /// first when the list lacks it ("Not set" for none). Before a list has been read,
    /// the value chosen now alone.
    static func items(_ list: ConfigOptions?, current: String) -> [Item] {
        let options = (list?.options ?? []).map { Item(value: $0.value, label: $0.label) }
        guard !options.contains(where: { $0.value == current }) else { return options }
        let chosen = Item(value: current, label: current.isEmpty ? notSet : current)
        return [chosen] + options
    }

    /// Other… is offered when the list allows a value of one's own — or when it could not be
    /// read at all, so the setting is never left with no way to change it.
    static func offersOther(_ loadable: Loadable<ConfigOptions>?) -> Bool {
        if let list = loadable?.value { return list.freeForm }
        return loadable?.error != nil
    }

    /// The line that says the menu is not the live list: the built-in list shown because the
    /// live one failed, or no list at all.
    /// `titles`: each setting's title by key, so a key the CLI names ("OPENAI_API_KEY is
    /// not set") reads as the setting a person knows.
    static func note(_ loadable: Loadable<ConfigOptions>?, titles: [String: String] = [:]) -> String? {
        if let list = loadable?.value {
            guard !list.live, let error = list.error, !error.isEmpty else { return nil }
            return "Showing the built-in list — \(Wording.named(error, titles: titles))"
        }
        guard let error = loadable?.error else { return nil }
        return "Couldn't list the choices — \(error.reported ?? error.message)"
    }

    /// What the list says about `value` ("warm, American, female", "Lyra's voice"), when it
    /// says anything the label does not ("opus" under "Claude Opus") — and not "current",
    /// which the menu already shows by its place.
    static func detail(of value: String, in list: ConfigOptions?) -> String? {
        guard let option = list?.options.first(where: { $0.value == value }),
              let detail = option.detail, !detail.isEmpty, detail != "current",
              !option.label.localizedCaseInsensitiveContains(detail) else { return nil }
        return Wording.capitalised(detail)
    }
}
