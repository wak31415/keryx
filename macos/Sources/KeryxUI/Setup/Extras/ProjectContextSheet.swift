import AppKit
import KeryxKit
import SwiftUI

/// Brief Lyra on Your Projects: the terminal wizard's project context, as a sheet, in its
/// three calls — the question (`setup project-context --describe`), the agent reading the
/// projects and drafting a brief of each (the stream, which can be cancelled), and what the
/// owner keeps of the draft, summaries as edited (`--apply`, on stdin). Nothing is kept until
/// then. "No" records the section as walked, as the wizard does.
package struct ProjectContextSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var question: ProjectContextQuestion?
    @State private var readError: KeryxError?
    @State private var choice = "explore"
    @State private var folders: [String] = []
    /// The draft being checked: each project kept or not, its summary as edited.
    @State private var kept: [String: Bool] = [:]
    @State private var summaries: [String: String] = [:]
    @State private var keptFacts: [String: Bool] = [:]
    @State private var result: ProjectContextResult?
    @State private var working = false
    @State private var failure: KeryxError?

    /// The catalog's: the question already read.
    package init(question: ProjectContextQuestion? = nil) {
        _question = State(initialValue: question)
    }

    package var body: some View {
        let run = model.projectDraftRun
        VStack(alignment: .leading, spacing: 0) {
            Text("Brief \(WizardWording.assistant(WizardFacts(model), status: model.status.value)) on Your Projects")
                .font(.headline)
                .padding([.horizontal, .top], 20)
            Form {
                if let result {
                    outcome(result)
                } else if let draft = run.result {
                    review(draft)
                } else if run.phase != nil {
                    progress(run)
                } else if let question {
                    ask(question)
                } else if let readError {
                    Section { ErrorText(readError.said("The question couldn't be read.")) }
                } else {
                    Section { StatusLine(tone: .working, text: "Reading what is kept already…") }
                }
                if let failure {
                    Section { ErrorText(failure.said("Nothing was kept.")) }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            buttons(run)
                .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 540, height: 580)
        .task { if question == nil { await read() } }
        .onChange(of: run.result) { _, draft in if let draft { seed(draft) } }
        .onDisappear {
            // Nothing else could finish it: stopped with the sheet, and its draft forgotten.
            if run.isRunning { run.cancel() } else { run.dismiss() }
        }
    }

    // MARK: - The question

    @ViewBuilder private func ask(_ question: ProjectContextQuestion) -> some View {
        Section {
            Text(question.note)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !question.summaries.isEmpty {
                Text("Briefs kept already: \(Wording.list(question.summaries)).")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        Section(question.question) {
            Picker(question.question, selection: $choice) {
                ForEach(question.choices) { option in
                    VStack(alignment: .leading, spacing: 1) {
                        Text(Wording.capitalised(option.label))
                        if let hint = option.hint {
                            Text(Wording.capitalised(hint)).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .tag(option.value)
                }
            }
            .pickerStyle(.radioGroup)
            .labelsHidden()
            if choice == "folders" {
                ForEach(folders, id: \.self) { folder in
                    LabeledContent(WizardPaths.short(folder)) {
                        Button("Remove") { folders.removeAll { $0 == folder } }
                    }
                }
                Button("Choose Folders…") { chooseFolders() }
            }
        }
    }

    // MARK: - The draft

    @ViewBuilder private func progress(_ run: StreamRun<ProjectDraft>) -> some View {
        Section {
            switch run.phase {
            case let .running(activity)?:
                VStack(alignment: .leading, spacing: 6) {
                    StatusLine(tone: .working, text: activity.message)
                    if let detail = activity.detail.map(Self.progress) {
                        Text(detail)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .truncationMode(.tail)
                    }
                }
            case .cancelling?:
                StatusLine(tone: .working, text: "Stopping…")
            case let .failed(failure)?:
                ErrorText(failure)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder private func review(_ draft: ProjectDraft) -> some View {
        Section {
            Text(draft.summary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if !draft.projects.isEmpty {
            Section("Projects") {
                ForEach(draft.projects) { project in
                    VStack(alignment: .leading, spacing: 4) {
                        Toggle(isOn: Binding { kept[project.name] ?? true } set: { kept[project.name] = $0 }) {
                            Text(project.name).font(.callout.weight(.semibold))
                            if project.addsProject {
                                Text("New to Keryx")
                            }
                        }
                        .toggleStyle(.checkbox)
                        TextEditor(text: Binding { summaries[project.name] ?? project.summary } set: {
                            summaries[project.name] = $0
                        })
                        .font(.callout)
                        .scrollContentBackground(.hidden)
                        .padding(4)
                        .background(.quinary, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .frame(height: 60)
                        .disabled(!(kept[project.name] ?? true))
                    }
                }
            }
        }
        if !draft.facts.isEmpty {
            Section("About You") {
                ForEach(draft.facts, id: \.self) { fact in
                    Toggle(fact, isOn: Binding { keptFacts[fact] ?? true } set: { keptFacts[fact] = $0 })
                        .toggleStyle(.checkbox)
                }
            }
        }
        if !draft.dropped.isEmpty {
            Section {
                Text("Left out, past what a call reads: \(Wording.list(draft.dropped)).")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private func outcome(_ result: ProjectContextResult) -> some View {
        Section {
            if result.problems.isEmpty {
                StatusLine(tone: .ok, text: Self.kept(result))
            } else {
                ForEach(result.problems) { StatusLine(tone: .problem, text: $0.message) }
            }
        }
    }

    /// The agent's progress as a person reads it: "[tool] Read README.md" → "Reading README.md".
    static func progress(_ detail: String) -> String {
        var line = detail.hasPrefix("[tool] ") ? String(detail.dropFirst("[tool] ".count)) : detail
        if line.hasPrefix("Read ") { line = "Reading " + line.dropFirst("Read ".count) }
        return line
    }

    /// "Kept 2 briefs and 1 thing about you."
    static func kept(_ result: ProjectContextResult) -> String {
        let briefs = result.kept.count == 1 ? "1 brief" : "\(result.kept.count) briefs"
        let facts = result.factsAdded == 1 ? "1 thing about you" : "\(result.factsAdded) things about you"
        return "Kept \(briefs) and \(facts)."
    }

    // MARK: - Buttons

    @ViewBuilder private func buttons(_ run: StreamRun<ProjectDraft>) -> some View {
        HStack {
            if working { ProgressView().controlSize(.small) }
            Spacer()
            if result != nil {
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            } else if let draft = run.result {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Keep") { keep(draft) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working)
            } else if run.isBusy {
                Button("Cancel") { run.cancel() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(!run.isRunning)
            } else if run.failure != nil {
                Button("Close") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Try Again") { run.dismiss() }
                    .keyboardShortcut(.defaultAction)
            } else {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(choice == "no" ? "Save" : "Start") { start() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(question == nil || working || (choice == "folders" && folders.isEmpty))
            }
        }
    }

    // MARK: - Doing

    private func read() async {
        switch await model.projectContextQuestion() {
        case let .success(found):
            question = found
            choice = found.choices.first?.value ?? "explore"
        case let .failure(error):
            readError = error
        }
    }

    private func start() {
        failure = nil
        switch choice {
        case "no":
            working = true
            Task {
                defer { working = false }
                if let error = await model.markSetupStep("projects") { failure = error } else { dismiss() }
            }
        case "folders":
            model.draftProjectContext(.folders(folders))
        default:
            model.draftProjectContext(.explore)
        }
    }

    private func seed(_ draft: ProjectDraft) {
        kept = Dictionary(uniqueKeysWithValues: draft.projects.map { ($0.name, true) })
        summaries = Dictionary(uniqueKeysWithValues: draft.projects.map { ($0.name, $0.summary) })
        keptFacts = Dictionary(draft.facts.map { ($0, true) }, uniquingKeysWith: { first, _ in first })
    }

    private func keep(_ draft: ProjectDraft) {
        let projects = draft.projects.filter { kept[$0.name] ?? true }.map { project in
            ProjectContextKeep.Project(name: project.name, path: project.path,
                                       summary: (summaries[project.name] ?? project.summary)
                                           .trimmingCharacters(in: .whitespacesAndNewlines))
        }
        let keep = ProjectContextKeep(folders: draft.folders, projects: projects,
                                      facts: draft.facts.filter { keptFacts[$0] ?? true })
        working = true
        failure = nil
        Task {
            defer { working = false }
            switch await model.applyProjectContext(keep) {
            case let .success(found): result = found
            case let .failure(error): failure = error
            }
        }
    }

    private func chooseFolders() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Choose"
        guard panel.runModal() == .OK else { return }
        for url in panel.urls where !folders.contains(url.path) { folders.append(url.path) }
    }
}
