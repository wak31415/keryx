import KeryxKit
import SwiftUI

/// About You: the owner's name (and, if they like, another name for the assistant), and the
/// phones they call from. Continue writes only what changed, with `config set`:
/// `OWNER_NAME`, `ASSISTANT_NAME`, `ALLOWED_CALLERS` and `OWNER_NUMBER` (the first number).
/// A blank name is "the owner"; at least one number is needed.
package struct AboutYouFrame: View {
    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var name: String
    @State private var assistant: String
    @State private var numbers: [String]
    @State private var renaming: Bool
    /// The sections the owner asked to change, though they are set.
    @State private var changing: Set<String> = []
    @State private var saving = false
    @State private var failure: KeryxError?
    @FocusState private var focus: Int?

    /// From what is saved, or — the catalog's — what is typed already.
    package init(facts: WizardFacts? = nil, typed: AboutYouAnswers? = nil) {
        let saved = facts.map { AboutYouAnswers(name: $0.text("OWNER_NAME") ?? "",
                                                assistant: $0.text("ASSISTANT_NAME") ?? "",
                                                numbers: CallerNumbers.saved($0)) }
        let start = typed ?? saved ?? AboutYouAnswers(name: "", assistant: "", numbers: [])
        _name = State(initialValue: start.name)
        _assistant = State(initialValue: start.assistant)
        _numbers = State(initialValue: start.numbers.isEmpty ? [""] : start.numbers)
        _renaming = State(initialValue: typed.map { !$0.assistant.isEmpty && $0.assistant != "Lyra" } ?? false)
        _seeded = State(initialValue: facts != nil || typed != nil)
    }

    /// Filled in from the settings once they are read (a fresh frame reads them as it opens).
    @State private var seeded: Bool

    package var body: some View {
        let facts = WizardFacts(model)
        let who = WizardWording.assistant(facts, status: model.status.value)
        let answers = AboutYouAnswers(name: name, assistant: assistant, numbers: numbers)
        let ready = CallerNumbers.list(numbers) != nil
            && (assistant.isEmpty || AboutYouAnswers.assistantProblem(assistant) == nil)
        WizardPage(symbol: .system("person.crop.circle"), title: WizardWording.aboutTitle(who),
                   sentence: WizardWording.aboutSentence(who), position: wizard.position(facts)) {
            VStack(spacing: 12) {
                nameSection(facts)
                phoneSection(facts, assistant: who)
                if let failure {
                    ErrorText(failure.said("Your details weren't saved."))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        } trailing: {
            if saving { ProgressView().controlSize(.small) }
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
                .disabled(saving)
            Button("Continue") { save(answers, facts: facts) }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!ready || saving)
        }
        .onAppear(perform: seed)
        .onChange(of: model.config.value == nil) { seed() }
    }

    // MARK: - Sections

    @ViewBuilder private func nameSection(_ facts: WizardFacts) -> some View {
        if folded("name", set: facts.text("OWNER_NAME") != nil) {
            FoldedSection(title: "Your Name", summary: summaryName(facts)) { changing.insert("name") }
        } else {
            WizardSection(title: "Your Name") {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("Your name", text: $name, prompt: Text(WizardWording.ownerPlaceholder))
                        .textFieldStyle(.roundedBorder)
                        .labelsHidden()
                    DisclosureGroup("Call the assistant something else", isExpanded: $renaming) {
                        assistantField(facts)
                            .padding(.top, 6)
                    }
                    .font(.callout)
                }
            }
        }
    }

    /// The assistant's name: the CLI's list when it offers one (SEAM: `config options
    /// ASSISTANT_NAME`), else a field.
    @ViewBuilder private func assistantField(_ facts: WizardFacts) -> some View {
        let problem = assistant.isEmpty ? nil : AboutYouAnswers.assistantProblem(assistant)
        VStack(alignment: .leading, spacing: 4) {
            if facts.row("ASSISTANT_NAME")?.hasOptions == true {
                OptionMenu(key: "ASSISTANT_NAME", title: "Assistant's name",
                           current: assistant.isEmpty ? (facts.text("ASSISTANT_NAME") ?? "") : assistant) {
                    assistant = $0
                }
            } else {
                TextField("Assistant's name", text: $assistant,
                          prompt: Text(facts.text("ASSISTANT_NAME") ?? "Lyra"))
                    .textFieldStyle(.roundedBorder)
                    .labelsHidden()
                    .frame(maxWidth: 220, alignment: .leading)
            }
            if let problem {
                Text(problem).font(.caption).foregroundStyle(.red)
            }
        }
    }

    @ViewBuilder private func phoneSection(_ facts: WizardFacts, assistant who: String) -> some View {
        if folded("phone", set: facts.row("ALLOWED_CALLERS")?.set == true && !CallerNumbers.saved(facts).isEmpty) {
            FoldedSection(title: "Your Phone", summary: CallerNumbers.saved(facts).joined(separator: ", ")) {
                changing.insert("phone")
            }
        } else {
            WizardSection(title: "Your Phone") {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(numbers.indices, id: \.self) { index in
                        numberRow(index, assistant: who)
                    }
                    if numbers.count < 5 {
                        Button {
                            numbers.append("")
                            focus = numbers.count - 1
                        } label: {
                            Label("Add Another", systemImage: "plus.circle")
                        }
                        .buttonStyle(.borderless)
                        .disabled(numbers.last?.trimmingCharacters(in: .whitespaces).isEmpty ?? true)
                    }
                }
            }
        }
    }

    private func numberRow(_ index: Int, assistant who: String) -> some View {
        let problem = CallerNumbers.problem(numbers[index])
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                TextField("Phone number", text: $numbers[index], prompt: Text("+44 7700 900123"))
                    .textFieldStyle(.roundedBorder)
                    .labelsHidden()
                    .focused($focus, equals: index)
                    .frame(width: 220)
                if index == 0 {
                    Text(WizardWording.ringsThisOne(who))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Button {
                        numbers.remove(at: index)
                    } label: {
                        Image(systemName: "minus.circle")
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Remove This Number")
                }
            }
            if let problem {
                Text(problem).font(.caption).foregroundStyle(.red)
            }
        }
    }

    // MARK: - State

    /// Folded: first-run, set, and not asked to change.
    private func folded(_ section: String, set: Bool) -> Bool {
        wizard.mode == .firstRun && set && !changing.contains(section)
    }

    private func summaryName(_ facts: WizardFacts) -> String {
        let owner = facts.text("OWNER_NAME") ?? WizardWording.ownerPlaceholder
        guard facts.row("ASSISTANT_NAME")?.set == true, let other = facts.text("ASSISTANT_NAME") else { return owner }
        return "\(owner) · the assistant is \(other)"
    }

    /// The fields from the settings, once, when they are read.
    private func seed() {
        guard !seeded, model.config.value != nil else { return }
        seeded = true
        let facts = WizardFacts(model)
        name = facts.text("OWNER_NAME") ?? ""
        assistant = facts.text("ASSISTANT_NAME") ?? ""
        let saved = CallerNumbers.saved(facts)
        numbers = saved.isEmpty ? [""] : saved
    }

    private func save(_ answers: AboutYouAnswers, facts: WizardFacts) {
        saving = true
        failure = nil
        Task {
            defer { saving = false }
            if let error = await WizardWrite.apply(answers.writes(from: facts), with: model) {
                failure = error
                return
            }
            await wizard.moveOn(model)
        }
    }
}
