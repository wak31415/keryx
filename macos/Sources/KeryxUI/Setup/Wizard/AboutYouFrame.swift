import KeryxKit
import SwiftUI

/// About You: the owner's name (and, if they like, another name for the assistant), the
/// phones they call from, and the PIN. Continue writes only what changed, with `config set`
/// (`OWNER_NAME`, `ASSISTANT_NAME`, `ALLOWED_CALLERS`, `OWNER_NUMBER`), then the PIN: typed
/// twice here and sent on `pin set --stdin`, or left to the first call (`setup mark pin`).
/// A PIN that works is changed only through the PIN sheet and its two confirmations. The
/// digits live only in this frame's two fields, are never shown or logged, and are emptied
/// once saved and whenever the frame goes. A blank name is "the owner"; at least one number
/// is needed.
package struct AboutYouFrame: View {
    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @State private var name: String
    @State private var assistant: String
    @State private var numbers: [String]
    @State private var renaming: Bool
    @State private var pinChoice: PinChoice = .now
    @State private var first: String
    @State private var second: String
    @State private var changingPin = false
    /// The sections the owner asked to change, though they are set.
    @State private var changing: Set<String> = []
    @State private var saving = false
    @State private var failure: KeryxError?
    /// A refusal of the digits themselves, said under the fields.
    @State private var digitsProblem: String?
    @FocusState private var focus: Int?

    /// From what is saved, or — the catalog's — what is typed already (made-up digits,
    /// shown as dots).
    package init(facts: WizardFacts? = nil, typed: AboutYouAnswers? = nil, pin: String = "") {
        let saved = facts.map { AboutYouAnswers(name: $0.text("OWNER_NAME") ?? "",
                                                assistant: $0.text("ASSISTANT_NAME") ?? "",
                                                numbers: CallerNumbers.saved($0).map(PhoneDisplay.format)) }
        let start = typed ?? saved ?? AboutYouAnswers(name: "", assistant: "", numbers: [])
        _name = State(initialValue: start.name)
        _assistant = State(initialValue: start.assistant)
        _numbers = State(initialValue: start.numbers.isEmpty ? [""] : start.numbers)
        _renaming = State(initialValue: typed.map { !$0.assistant.isEmpty && $0.assistant != "Lyra" } ?? false)
        _seeded = State(initialValue: facts != nil || typed != nil)
        _first = State(initialValue: pin)
        _second = State(initialValue: pin)
    }

    /// Filled in from the settings once they are read (a fresh frame reads them as it opens).
    @State private var seeded: Bool

    package var body: some View {
        let facts = WizardFacts(model)
        let who = WizardWording.assistant(facts, status: model.status.value)
        let answers = AboutYouAnswers(name: name, assistant: assistant, numbers: numbers)
        let numbersReady = CallerNumbers.list(numbers) != nil
        let nameReady = assistant.isEmpty || AboutYouAnswers.assistantProblem(assistant) == nil
        let pinReady = PinRules.answered(model.pin.value, choice: pinChoice, first: first, second: second,
                                         folded: folded("pin", set: facts.done("pin")))
        let numberWrong = numbers.contains { CallerNumbers.problem($0) != nil }
        WizardPage(symbol: .system("person.crop.circle"), title: WizardWording.aboutTitle,
                   sentence: WizardWording.aboutSentence, position: wizard.position(facts)) {
            // A third number, or an error under each, may not fit: the sections scroll then.
            ScrollView {
                VStack(spacing: 10) {
                    nameSection(facts)
                    phoneSection(facts, assistant: who)
                    pinSection(facts, assistant: who)
                    if let failure {
                        ErrorText(failure.said("Your details weren't saved."))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .scrollBounceBehavior(.basedOnSize)
        } leading: {
            if numberWrong {
                WaitingCaption(text: "Fix the number to continue")
            } else if !numbersReady {
                WaitingCaption(text: "Add a number to continue")
            } else if !pinReady {
                WaitingCaption(text: "Type the PIN twice, or set it on your first call")
            }
        } trailing: {
            if saving { ProgressView().controlSize(.small) }
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
                .disabled(saving)
            Button("Continue") { save(answers, facts: facts) }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!numbersReady || !nameReady || !pinReady || saving)
        }
        .onAppear(perform: seed)
        .onChange(of: model.config.value == nil) { seed() }
        .onChange(of: first) { digitsProblem = nil }
        .onChange(of: second) { digitsProblem = nil }
        .onDisappear(perform: forgetDigits)
        .pinSheet(isPresented: $changingPin)
        .task { await model.refresh(.pin) }
    }

    // MARK: - Your name

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

    // MARK: - Your phone

    @ViewBuilder private func phoneSection(_ facts: WizardFacts, assistant who: String) -> some View {
        let saved = CallerNumbers.saved(facts)
        if folded("phone", set: facts.row("ALLOWED_CALLERS")?.set == true && !saved.isEmpty) {
            FoldedSection(title: "Your Phone", summary: saved.map(PhoneDisplay.format).joined(separator: ", ")) {
                changing.insert("phone")
            }
        } else {
            WizardSection(title: "Your Phone") {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(numbers.indices, id: \.self) { index in
                        numberRow(index, assistant: who)
                    }
                    HStack {
                        if numbers.count < 5 {
                            Button {
                                numbers.append("")
                                focus = numbers.count - 1
                            } label: {
                                Label("Add Another", systemImage: "plus.circle")
                            }
                            .buttonStyle(.borderless)
                        }
                        Spacer()
                        Text(WizardWording.callersCaption(who))
                            .font(.caption)
                            .foregroundStyle(.secondary)
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

    // MARK: - Your PIN

    @ViewBuilder private func pinSection(_ facts: WizardFacts, assistant who: String) -> some View {
        if let status = model.pin.value {
            if !PinRules.asksForDigits(status) {
                pinState(status, kind: PinWording.kind(status), assistant: who)
            } else if folded("pin", set: facts.done("pin")) {
                // Left to the first call before: said, and changed by choosing again.
                FoldedSection(title: "Your PIN", summary: WizardWording.pinLaterCaption(who)) {
                    changing.insert("pin")
                }
            } else {
                pinChoiceSection(assistant: who)
            }
        } else if let error = model.pin.error {
            WizardSection(title: "Your PIN") { ErrorText(error.said("Keryx couldn't say whether a PIN is set.")) }
        } else {
            WizardSection(title: "Your PIN") { StatusLine(tone: .working, text: "Checking the PIN…") }
        }
    }

    /// A PIN that works, one Keryx was started with, or one still with Jarvis's files.
    @ViewBuilder private func pinState(_ status: PinStatus, kind: PinWording.Kind, assistant who: String) -> some View {
        switch kind {
        case .set:
            FoldedSection(title: "Your PIN", summary: "A PIN is set", changeTitle: "Change PIN…") { changingPin = true }
        case .unusable:
            // Not replaced inline: the sheet asks "Replace the PIN?" before it does.
            WizardSection(title: "Your PIN") {
                HStack {
                    StatusLine(tone: .warning, text: "The PIN file holds no PIN")
                    Spacer()
                    Button("Choose PIN…") { changingPin = true }
                        .controlSize(.regular)
                }
            }
        case .environment(usable: true):
            FoldedSection(title: "Your PIN", summary: WizardWording.pinStartedWithTitle)
                .help(WizardWording.pinStartedWith)
        case .environment(usable: false):
            WizardSection(title: "Your PIN") {
                VStack(alignment: .leading, spacing: 2) {
                    StatusLine(tone: .warning, text: WizardWording.pinStartedWithBadTitle)
                    Text(WizardWording.pinStartedWithBad)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        default:
            WizardSection(title: "Your PIN") {
                StatusLine(tone: .none, text: PinWording.banner(status, assistant: who).text)
            }
        }
    }

    private func pinChoiceSection(assistant who: String) -> some View {
        WizardSection(title: "Your PIN") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Your PIN", selection: $pinChoice) {
                    Text(WizardWording.pinNow).tag(PinChoice.now)
                    Text(WizardWording.pinLater).tag(PinChoice.firstCall)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                if pinChoice == .now {
                    HStack(spacing: 8) {
                        SecureField("PIN", text: $first, prompt: Text("PIN"))
                            .textFieldStyle(.roundedBorder)
                            .labelsHidden()
                            .frame(width: 120)
                        SecureField("PIN again", text: $second, prompt: Text("Again"))
                            .textFieldStyle(.roundedBorder)
                            .labelsHidden()
                            .frame(width: 120)
                        if let problem = digitsProblem ?? PinWording.problem(first: first, second: second) {
                            Text(problem).font(.caption).foregroundStyle(.red)
                        }
                    }
                    Text(WizardWording.pinCaption(who))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(WizardWording.pinLaterCaption(who))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
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
        let saved = CallerNumbers.saved(facts).map(PhoneDisplay.format)
        numbers = saved.isEmpty ? [""] : saved
        // Left to the first call before: that is the answer until changed.
        if facts.done("pin") { pinChoice = .firstCall }
    }

    private func forgetDigits() {
        first = ""
        second = ""
    }

    private func save(_ answers: AboutYouAnswers, facts: WizardFacts) {
        let status = model.pin.value
        let pin = PinRules.save(status, choice: pinChoice, folded: folded("pin", set: facts.done("pin")),
                                stepDone: facts.done("pin"))
        saving = true
        failure = nil
        Task {
            defer { saving = false }
            switch await AboutYouSave.run(answers.writes(from: facts), pin: pin, digits: first, status: status,
                                          model: model) {
            case .saved:
                forgetDigits()
                await wizard.moveOn(model)
            case let .failed(error):
                failure = error
            case let .digits(problem):
                digitsProblem = problem
            }
        }
    }
}
