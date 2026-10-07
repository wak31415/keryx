import AppKit
import KeryxKit
import SwiftUI

/// One session from first to last, the way Messages shows a conversation: what was asked,
/// what the agent did, each follow-up where it came, and the result. Newest at the bottom,
/// and it opens there.
struct SessionTimeline: View {
    /// The session as the list (or its detail, whichever is newer) last saw it.
    let task: TaskItem
    /// Its log and report; `nil` until a load has begun.
    let detail: Loadable<TaskDetail>?
    /// The assistant's name, for the sentence about being told.
    let assistant: String

    @Environment(AppModel.self) private var model

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    Divider()
                    OutgoingBubble(title: "You asked", time: SessionWording.time(task.createdAt),
                                   text: Wording.capitalised(task.description))
                    log
                    ending
                    Color.clear.frame(height: 0).id(Self.bottom)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 20)
                .frame(maxWidth: 760, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            // Opens at the newest, and follows new steps; a short session stays at the top.
            .onAppear { scrollToBottom(proxy) }
            .onChange(of: detail?.value?.log.count) { scrollToBottom(proxy) }
            .onChange(of: task.status) { scrollToBottom(proxy) }
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        Task { @MainActor in proxy.scrollTo(Self.bottom, anchor: .bottom) }
    }

    private static let bottom = "bottom"
    private var agent: String { Wording.agent(task.agent) }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Wording.capitalised(task.description))
                .font(.title3.weight(.semibold))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text(SessionWording.headerByline(task))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            StatusLabel(task: task)
        }
    }

    // MARK: - Log

    @ViewBuilder private var log: some View {
        if let value = detail?.value {
            if value.logTruncated {
                Text("Earlier steps aren't shown")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
            }
            ForEach(SessionWording.stretches(value.log)) { stretch in
                switch stretch {
                case let .steps(entries):
                    StepsBox(agent: agent, entries: entries)
                case let .followup(entry):
                    OutgoingBubble(title: "You followed up", time: entry.time, text: Wording.capitalised(entry.text))
                }
            }
        } else if let error = detail?.error, detail?.isLoading != true {
            HStack(alignment: .firstTextBaseline) {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Couldn't load the session's steps")
                        Text(error.message)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                } icon: {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                }
                Spacer()
                Button("Try Again") { Task { await model.loadTask(task.id) } }
            }
        } else {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Loading the session…").foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
        }
    }

    // MARK: - Ending

    @ViewBuilder private var ending: some View {
        switch task.status {
        case .queued:
            working("Waiting for \(agent) to start…")
        case .running:
            working("\(agent) is working…")
        case .done, .failed:
            result
        case .cancelled:
            Label("Cancelled", systemImage: "minus.circle")
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
        }
        if task.told == false {
            Label("Reading it here doesn't count as being told — \(assistant) will still tell you on the phone.",
                  systemImage: "phone")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func working(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).foregroundStyle(.secondary)
        }
    }

    private var result: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Result")
                .font(.headline)
            if let summary = task.summary, !summary.isEmpty {
                Text(Self.inline(Wording.capitalised(summary)))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if task.status == .failed, let error = task.error, !error.isEmpty {
                Text(Wording.capitalised(error))
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let report = detail?.value?.report, !report.isEmpty {
                GroupBox {
                    ReportText(markdown: report)
                        .padding(6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if detail?.value?.reportTruncated == true, let path = task.reportPath {
                HStack(spacing: 10) {
                    Text("Report truncated")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Open Full Report") {
                        NSWorkspace.shared.open(URL(fileURLWithPath: path))
                    }
                }
            }
            if let usage = SessionWording.usage(task) {
                Text(usage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
    }

    /// Inline Markdown (code, emphasis, links), keeping the text's own line breaks.
    static func inline(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }
}

// MARK: - Pieces

/// The session's status in its colour: "Done in 15 minutes".
private struct StatusLabel: View {
    let task: TaskItem

    var body: some View {
        Label(SessionWording.statusTitle(task), systemImage: SessionWording.symbol(task.status))
            .foregroundStyle(colour)
            .font(.callout)
    }

    private var colour: AnyShapeStyle {
        switch task.status {
        case .done: AnyShapeStyle(.green)
        case .failed: AnyShapeStyle(.red)
        case .running: AnyShapeStyle(.tint)
        case .queued, .cancelled: AnyShapeStyle(.secondary)
        }
    }
}

/// Something the owner said, on the right like an outgoing message.
private struct OutgoingBubble: View {
    let title: String
    let time: String?
    let text: String

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            HStack(spacing: 6) {
                Text(title)
                if let time { Text(time).monospacedDigit() }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            Text(text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 80)
        .accessibilityElement(children: .combine)
    }
}

/// A run of the agent's steps, each a time and what it did; folded to the last few when
/// there are many.
private struct StepsBox: View {
    let agent: String
    let entries: [SessionLogEntry]

    @State private var expanded = false

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                if folds && !expanded {
                    Button("Show All \(entries.count) Steps") { expanded = true }
                        .buttonStyle(.link)
                        .font(.callout)
                }
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 6) {
                    ForEach(shown) { entry in
                        GridRow {
                            // A log written without times leaves out the column.
                            if timed {
                                Text(entry.time ?? "")
                                    .font(.callout)
                                    .monospacedDigit()
                                    .foregroundStyle(.secondary)
                            }
                            Text(Wording.capitalised(entry.text))
                                .font(.callout)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            .padding(6)
        } label: {
            Text("\(agent) worked")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
        }
    }

    private var folds: Bool { entries.count > SessionWording.foldAbove }
    private var timed: Bool { entries.contains { $0.time != nil } }

    private var shown: [SessionLogEntry] {
        folds && !expanded ? Array(entries.suffix(SessionWording.foldedCount)) : entries
    }
}

/// An agent's report: its headings bold, its paragraphs with their inline Markdown, a code
/// block in a monospaced face. Only what a report uses; never HTML.
struct ReportText: View {
    let markdown: String

    enum Block: Equatable {
        case heading(String)
        case paragraph(String)
        case code(String)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(Self.blocks(markdown).enumerated()), id: \.offset) { _, block in
                switch block {
                case let .heading(text):
                    Text(SessionTimeline.inline(text)).font(.headline)
                case let .paragraph(text):
                    Text(SessionTimeline.inline(text))
                case let .code(text):
                    Text(text).font(.callout.monospaced())
                }
            }
        }
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
    }

    /// The report split at blank lines: a line of `#`s is a heading, a fenced block is code,
    /// a rule (`---`, `* * *`) only separates, and a list's `-` or `*` marks become bullets.
    static func blocks(_ markdown: String) -> [Block] {
        var blocks: [Block] = []
        var lines: [String] = []
        var inCode = false

        func flush() {
            let text = lines.joined(separator: "\n").trimmingCharacters(in: .newlines)
            lines = []
            guard !text.isEmpty else { return }
            blocks.append(.paragraph(text))
        }

        for raw in markdown.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("```") {
                if inCode {
                    blocks.append(.code(lines.joined(separator: "\n")))
                    lines = []
                } else {
                    flush()
                }
                inCode.toggle()
                continue
            }
            if inCode {
                lines.append(raw)
            } else if line.isEmpty || isRule(line) {
                flush()
            } else if line.hasPrefix("#") {
                flush()
                blocks.append(.heading(line.drop { $0 == "#" }.trimmingCharacters(in: .whitespaces)))
            } else if line.hasPrefix("- ") || line.hasPrefix("* ") {
                lines.append("•  " + line.dropFirst(2))
            } else {
                lines.append(raw)
            }
        }
        if inCode {
            blocks.append(.code(lines.joined(separator: "\n")))
        } else {
            flush()
        }
        return blocks
    }

    /// Three or more of one of `-`, `*` or `_`, and nothing else but spaces: Markdown's rule.
    private static func isRule(_ line: String) -> Bool {
        let marks = line.filter { $0 != " " }
        guard marks.count >= 3, let first = marks.first, "-*_".contains(first) else { return false }
        return marks.allSatisfy { $0 == first }
    }
}
