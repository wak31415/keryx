import KeryxKit
import SwiftUI

/// Tell Lyra About Yourself: the terminal wizard's "About you", as a sheet — what Lyra is
/// mostly for, anything else, and a few things to know — written as the assistant's first
/// memory through the wizard's own code (`setup profile`). The answers go to the CLI as one
/// JSON object on stdin. It shows the memory before it is written (`--preview`), then
/// writes it (`--apply`); a memory that exists is kept unless the owner says to replace it.
package struct ProfileSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var questions: ProfileQuestions?
    @State private var readError: KeryxError?
    @State private var replace = false
    @State private var uses: Set<String> = []
    @State private var other = ""
    @State private var facts = ""
    /// What `--preview` said the answers come to; shown before anything is written.
    @State private var preview: ProfileResult?
    @State private var result: ProfileResult?
    @State private var working = false
    @State private var failure: KeryxError?

    /// The catalog's: the questions already read, and answers given.
    package init(questions: ProfileQuestions? = nil, answers: ProfileAnswers? = nil, preview: ProfileResult? = nil) {
        _questions = State(initialValue: questions)
        _replace = State(initialValue: answers?.replace ?? false)
        _uses = State(initialValue: Set(answers?.uses ?? []))
        _other = State(initialValue: answers?.other ?? "")
        _facts = State(initialValue: answers?.facts.joined(separator: "\n") ?? "")
        _preview = State(initialValue: preview)
    }

    private var answers: ProfileAnswers {
        ProfileAnswers(replace: replace, uses: (questions?.question("uses")?.choices ?? []).map(\.value).filter(uses.contains),
                       other: other.trimmingCharacters(in: .whitespacesAndNewlines),
                       facts: facts.split(whereSeparator: \.isNewline)
                           .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty })
    }

    package var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.headline)
                .padding([.horizontal, .top], 20)
            Form {
                if let result {
                    outcome(result)
                } else if let preview {
                    review(preview)
                } else if let questions {
                    ask(questions)
                } else if let readError {
                    Section { ErrorText(readError.said("The questions couldn't be read.")) }
                } else {
                    Section { StatusLine(tone: .working, text: "Reading the questions…") }
                }
                if let failure {
                    Section { ErrorText(failure.said("Nothing was written.")) }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            buttons
                .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 520, height: 560)
        .task { if questions == nil { await read() } }
    }

    private var title: String {
        "Tell \(questions?.assistant ?? "the Assistant") About Yourself"
    }

    // MARK: - Asking

    @ViewBuilder private func ask(_ questions: ProfileQuestions) -> some View {
        Section {
            Text(Self.note(questions.note))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if questions.question("replace") != nil {
                Toggle("\(questions.assistant) already remembers a few things about you. Replace them with what you say here?",
                       isOn: $replace)
            }
        }
        if let question = questions.question("uses") {
            Section(question.prompt) {
                ForEach(question.choices ?? []) { choice in
                    Toggle(Wording.capitalised(choice.label), isOn: Binding {
                        uses.contains(choice.value)
                    } set: { on in
                        if on { uses.insert(choice.value) } else { uses.remove(choice.value) }
                    })
                    .toggleStyle(.checkbox)
                }
                if let other = questions.question("other") {
                    TextField(other.prompt, text: $other, prompt: Text("Anything else"))
                }
            }
        }
        if let question = questions.question("facts") {
            Section {
                TextEditor(text: $facts)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .padding(4)
                    .background(.quinary, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .frame(minHeight: 80)
            } header: {
                Text(question.prompt)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The memory as it would be written, and anything wrong with the answers.
    @ViewBuilder private func review(_ preview: ProfileResult) -> some View {
        if !preview.problems.isEmpty {
            Section {
                ForEach(preview.problems) { problem in
                    StatusLine(tone: .problem, text: problem.message)
                }
            }
        }
        if let text = preview.text {
            Section("What \(questions?.assistant ?? "the assistant") Will Remember") {
                ScrollView {
                    MemoryText(text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 220)
                Text("\(preview.chars.formatted()) of \(preview.maxChars.formatted()) characters; every call reads it.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else if preview.problems.isEmpty {
            Section {
                StatusLine(tone: .none, text: preview.status == "kept"
                    ? "What \(questions?.assistant ?? "the assistant") remembers now is kept: nothing would change."
                    : "Nothing was said, so nothing would be written.")
            }
        }
    }

    @ViewBuilder private func outcome(_ result: ProfileResult) -> some View {
        Section {
            if result.refused {
                ForEach(result.problems) { StatusLine(tone: .problem, text: $0.message) }
            } else if result.written {
                StatusLine(tone: .ok, text: "Saved. \(questions?.assistant ?? "The assistant") reads it at the top of every call.")
            } else {
                StatusLine(tone: .ok, text: "Nothing changed: what \(questions?.assistant ?? "the assistant") remembers is kept.")
            }
        }
    }

    /// The CLI's privacy note, with the provider named as the owner knows it.
    static func note(_ text: String) -> String {
        text.replacingOccurrences(of: "the realtime provider", with: "the voice provider")
    }

    // MARK: - Buttons

    @ViewBuilder private var buttons: some View {
        HStack {
            if working { ProgressView().controlSize(.small) }
            Spacer()
            if result != nil {
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            } else if let preview {
                Button("Back") { self.preview = nil }
                    .disabled(working)
                Button("Save") { apply() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || preview.text == nil || !preview.problems.isEmpty)
            } else {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Preview") { look() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || questions == nil)
            }
        }
    }

    // MARK: - Reading and writing

    private func read() async {
        switch await model.profileQuestions() {
        case let .success(found):
            questions = found
            replace = (found.question("replace")?.default == .bool(true))
        case let .failure(error):
            readError = error
        }
    }

    private func look() {
        working = true
        failure = nil
        Task {
            defer { working = false }
            switch await model.previewProfile(answers) {
            case let .success(found): preview = found
            case let .failure(error): failure = error
            }
        }
    }

    private func apply() {
        working = true
        failure = nil
        Task {
            defer { working = false }
            switch await model.applyProfile(answers) {
            case let .success(found): result = found
            case let .failure(error): failure = error
            }
        }
    }
}

/// The memory as a person reads it: its headings as headings and its facts as a list, with
/// no Markdown markers left in sight.
struct MemoryText: View {
    let text: String

    init(_ text: String) {
        self.text = text
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(Self.lines(text).enumerated()), id: \.offset) { _, line in
                switch line {
                case let .heading(words, level):
                    Text(words)
                        .font(level == 1 ? .headline : .subheadline.weight(.semibold))
                        .padding(.top, level == 1 ? 0 : 4)
                case let .item(words):
                    Text("•  " + words).font(.callout)
                case let .plain(words):
                    Text(words).font(.callout)
                }
            }
        }
        .textSelection(.enabled)
    }

    enum Line: Equatable {
        case heading(String, level: Int)
        case item(String)
        case plain(String)
    }

    static func lines(_ text: String) -> [Line] {
        text.split(separator: "\n").compactMap { raw in
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { return nil }
            if line.hasPrefix("#") {
                let level = line.prefix { $0 == "#" }.count
                return .heading(line.dropFirst(level).trimmingCharacters(in: .whitespaces), level: level)
            }
            if line.hasPrefix("- ") || line.hasPrefix("* ") { return .item(String(line.dropFirst(2))) }
            return .plain(line)
        }
    }
}
