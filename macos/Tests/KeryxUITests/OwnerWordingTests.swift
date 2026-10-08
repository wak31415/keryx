import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The owner never reads the word "service": Keryx runs in the background, and the unit, its
/// label and its manager are for a tooltip at most. Held two ways — every string literal in
/// the app's sources, as a grep would, and what the wording tables actually say.
@MainActor @Suite struct OwnerWordingTests {
    static let word = #/(?i)\bservices?\b/#

    /// Literals that may hold the word because they are not the app's words: an identifier
    /// the CLI uses (`service`, `service:install`), or a CLI title the app translates away.
    static func allowed(_ literal: String, in file: String) -> Bool {
        if literal.wholeMatch(of: #/[a-z0-9_.:\-]+/#) != nil { return true }
        // `CheckWording.titles`' keys: the CLI's old check titles, said otherwise.
        return file == "CheckLine.swift" && ["Service manager", "Service running"].contains(literal)
    }

    static func sources() -> [URL] {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources")
        return ["KeryxUI", "KeryxApp", "KeryxKit"].flatMap { module -> [URL] in
            let folder = root.appendingPathComponent(module)
            let found = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: nil)
            return (found?.compactMap { $0 as? URL } ?? []).filter { $0.pathExtension == "swift" }
        }
    }

    @Test func noStringLiteralInTheAppSaysService() throws {
        let files = Self.sources()
        #expect(files.count > 50, "the sources were found")
        var offenders: [String] = []
        for file in files {
            let text = try String(contentsOf: file, encoding: .utf8)
            for (number, line) in text.components(separatedBy: "\n").enumerated() {
                if line.trimmingCharacters(in: .whitespaces).hasPrefix("//") { continue }
                for match in line.matches(of: #/"(?:[^"\\]|\\.)*"/#) {
                    let literal = String(match.output.dropFirst().dropLast())
                    if literal.contains(Self.word), !Self.allowed(literal, in: file.lastPathComponent) {
                        offenders.append("\(file.lastPathComponent):\(number + 1) \(literal)")
                    }
                }
            }
        }
        #expect(offenders.isEmpty, "\(offenders)")
    }

    /// What the tables say, read back: every run's steps, every quit question, the settings'
    /// titles and sections from the CLI's own `config list`, the Overview's row.
    @Test func theWordingTablesNeverSayService() {
        var said: [String] = []
        for order in [RunSteps.modelServer, RunSteps.ollama, RunSteps.voice, RunSteps.voice(seen: ["voice_llm"]),
                      RunSteps.voiceLLMHere, RunSteps.voiceLLMRemote] {
            said += order.map(\.title)
        }
        for flags in 0..<(1 << 7) {
            let on = { (bit: Int) in flags & (1 << bit) != 0 }
            for change in [nil, AppModel.ServiceChange.installing, .uninstalling] {
                if let question = Quitting.question(
                    downloading: on(0), settingUpVoice: on(1), startingModelServer: on(2),
                    downloadingVoiceModels: on(3), changingVoiceLLM: on(4), startingVoiceLLMServer: on(5),
                    signingIn: on(6) ? "Claude" : nil, assistant: "Lyra", changingService: change,
                    writing: AppModel.Write.allCases, stopsAnswering: true, liveCalls: 1, confirm: true) {
                    said += [question.message, question.detail]
                }
            }
        }
        said += AppModel.Write.allCases.map(\.clause)
        for check in PreviewData.doctor.checks {
            said += [ChecklistWording.title(check), CheckWording.detail(ChecklistWording.detail(check)).text]
        }
        for plan in [PreviewData.setupPlan, PreviewData.setupPlanPartway, PreviewData.setupPlanReady] {
            said += plan.steps.map(WizardWording.stepTitle)
            said += plan.steps.map { WizardWording.checkRow(.step($0.id), plan: plan) }
        }
        for row in PreviewData.config {
            said += [row.shownTitle, row.shownGroupTitle, ConfigDescriptions.text(row), ConfigWording.tooltip(row)]
        }
        var stopped = PreviewData.status
        stopped.service.active = false
        stopped.health.reachable = false
        for step in [AnsweringStep.start, .waitingForSetup, .unknown] {
            for failure in [nil, KeryxError.failed(command: "keryx service start", exitCode: 1, message: "x")] {
                let line = ServiceWording.answering(stopped, step: step, change: nil, failure: failure)
                said += [line.text, line.hint, line.help].compactMap { $0 }
            }
        }
        said.append(ServiceWording.answering(stopped, step: .start, change: nil, failure: nil, finishingStop: true).help)
        let offenders = said.filter { $0.contains(Self.word) }
        #expect(offenders.isEmpty, "\(offenders)")
    }
}
