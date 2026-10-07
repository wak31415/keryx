import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

@Suite struct DecodingTests {
    // MARK: - Every fixture decodes

    @Test func statusFixtureDecodes() throws {
        let report = PreviewData.status
        #expect(report.schema == 1)
        #expect(report.assistant.name == "Lyra")
    }

    @Test func statusMigrationFixtureDecodes() throws {
        let report = PreviewData.statusMigration
        #expect(report.storage.migrationRequired == true)
        #expect(report.tasks.ok == false)
    }

    @Test func doctorFixtureDecodes() throws {
        let report = PreviewData.doctor
        #expect(report.checks.count >= 8)
        #expect(Set(report.checks.map(\.section)).count >= 4)
        #expect(report.checks.contains { $0.state == "missing" })
        #expect(report.checks.contains { $0.state == "failed" })
    }

    @Test func doctorCheckTitlesAreForPeople() throws {
        let key = try #require(PreviewData.doctor.checks.first { $0.name == "OPENAI_API_KEY" })
        #expect(key.title == "OpenAI API key")
        #expect(key.displayTitle == "OpenAI API key")
        #expect(key.id == "voice/OPENAI_API_KEY")
    }

    @Test func doctorCheckWithoutATitleStillDecodes() throws {
        let json = #"{"ok": true, "checks": [{"name": "tunnel", "detail": "/opt/homebrew/bin/cloudflared", "severity": "hard", "section": "phone", "state": "ok", "ok": true, "unset": false}]}"#
        let report = try JSON.decoder.decode(DoctorReport.self, from: Data(json.utf8))
        #expect(report.checks[0].title == nil)
        #expect(report.checks[0].displayTitle == "tunnel")
    }

    @Test func tasksFixtureDecodes() throws {
        let list = PreviewData.tasks
        #expect(list.tasks.count == 6)
        #expect(Set(list.tasks.map(\.status)) == Set(TaskStatus.allCases))
        #expect(list.tasks.contains { $0.internal })
        #expect(list.tasks.contains { $0.told == false })

        let task = list.tasks.first { $0.id == 40 }
        #expect(task?.createdAt == isoDate("2026-10-05T21:03:44+00:00"))
    }

    @Test func taskDetailFixtureDecodes() throws {
        let detail = PreviewData.taskDetail
        #expect(detail.report != nil)
        #expect(detail.report!.components(separatedBy: "\n\n").count >= 3)
        #expect(detail.reportTruncated == false)
        #expect(detail.log.count >= 12)
        #expect(detail.log.contains { $0.kind == .followup })
        // ids are assigned by position
        #expect(detail.log.map(\.id) == Array(0..<detail.log.count))
        #expect(detail.logTruncated == false)
        #expect(detail.resumable == false)
    }

    @Test func usageFixtureDecodes() throws {
        let report = PreviewData.usage
        #expect(report.since != nil)
        #expect(report.projects.count >= 2)
        #expect(report.projects.contains { $0.label == "(housekeeping)" })
    }

    @Test func modelsFixtureDecodes() throws {
        let report = PreviewData.models
        #expect(report.models.count == 8)
        #expect(report.models.filter(\.recommended).count == 1)
        #expect(report.models.filter(\.downloaded).count == 2)
        #expect(report.models.filter { !$0.fits }.count == 2)
        #expect(report.servers.llm.reachable == true)
        #expect(report.servers.llm.model != nil)
    }

    @Test func modelsEmptyServersFixtureDecodes() throws {
        let report = PreviewData.modelsEmptyServers
        #expect(report.servers.llm.model == nil)
        #expect(report.servers.llm.reachable == false)
        #expect(report.servers.localAgent.enabled == false)
    }

    @Test func configFixtureDecodes() throws {
        let rows = PreviewData.config
        #expect(rows.count >= 15)
        #expect(Set(rows.map(\.group)).count >= 5)
        #expect(rows.contains { $0.secret && $0.set })
        #expect(rows.contains { $0.secret && !$0.set })

        let vadMode = rows.first { $0.key == "VAD_MODE" }
        #expect(vadMode?.choices == ["server", "semantic"])
        #expect(rows.allSatisfy { $0.title != nil && $0.groupTitle != nil })
    }

    @Test func configRowNamesItselfForAPerson() throws {
        let row = try decodeConfigRow(#""title": "Model server port", "group_title": "Local and self-hosted models","#)
        #expect(row.displayTitle == "Model server port")
        #expect(row.displayGroupTitle == "Local and self-hosted models")
    }

    @Test func configRowFromAnOlderCLIFallsBackToTheKeyAndGroup() throws {
        let row = try decodeConfigRow("")
        #expect(row.title == nil)
        #expect(row.groupTitle == nil)
        #expect(row.displayTitle == "LLM_SERVER_PORT")
        #expect(row.displayGroupTitle == "Local Models")
        #expect(ConfigRow.title(ofGroup: "storage") == "Storage")
    }

    @Test func configRowSaysWhetherItHasOptionsAndAnOlderCLISaysNot() throws {
        #expect(try decodeConfigRow("").hasOptions == false)
        #expect(try decodeConfigRow(#""has_options": true,"#).hasOptions)
        let rows = PreviewData.config
        #expect(rows.first { $0.key == "OPENAI_REALTIME_MODEL" }?.hasOptions == true)
        #expect(rows.first { $0.key == "ASSISTANT_NAME" }?.hasOptions == false)
    }

    @Test func optionsTakeAnEmptyCurrentAndANullOne() throws {
        func decode(_ current: String) throws -> ConfigOptions {
            let json = """
            {"key": "CODEX_MODEL", "current": \(current), "options": [{"value": "", "label": "Codex's default",
             "detail": null}], "source": "codex", "live": true, "free_form": true, "error": null}
            """
            return try JSON.decoder.decode(ConfigOptions.self, from: Data(json.utf8))
        }
        // A key that lists a "" option says "" when unset; every other key says null.
        #expect(try decode(#""""#).current == "")
        #expect(try decode("null").current == nil)
        #expect(PreviewData.options["CODEX_MODEL"]?.current == "")
        #expect(PreviewData.options["OPENAI_VOICE"]?.current == "")
    }

    private func decodeConfigRow(_ titles: String) throws -> ConfigRow {
        let json = """
        {"key": "LLM_SERVER_PORT", "group": "local", \(titles) "description": "The port.",
         "source": "default", "type": "int", "required": false, "set": false, "secret": false,
         "service_writable": true, "protected": false, "value": 8090, "choices": null}
        """
        return try JSON.decoder.decode(ConfigRow.self, from: Data(json.utf8))
    }

    @Test func approvalsFixtureDecodes() throws {
        let report = PreviewData.approvals
        #expect(report.entries.count >= 4)
        #expect(report.cancelledBeforeCall == 2)
        #expect(report.answeredAfterCall == 1)
        // The broker numbers its requests; the `started` line carries none.
        #expect(report.entries.contains { if case .number = $0.requestId { return true }; return false })
        #expect(report.entries.contains { $0.event == "started" && $0.requestId == nil })
    }

    /// The broker writes an int, but `request_id` is whatever the audit line holds: a string
    /// survives as one rather than failing the whole document.
    @Test func approvalEntryKeepsAStringRequestId() throws {
        let json = #"{"ts": "2026-10-05T10:40:55+00:00", "event": "raised", "request_id": "ask-2", "what": "x"}"#
        let entry = try JSON.decoder.decode(ApprovalEntry.self, from: Data(json.utf8))
        #expect(entry.requestId == .string("ask-2"))
    }

    @Test func memoryFixtureDecodes() throws {
        let document = PreviewData.memory
        #expect(document.exists == true)
        #expect(document.text != nil)
    }

    // MARK: - Unknown keys are ignored

    @Test func decodingToleratesAnUnknownKey() throws {
        let json = """
        {"path": "/tmp/memory.md", "exists": true, "text": "hi", "a_future_field": 42}
        """
        let document = try JSON.decoder.decode(MemoryDocument.self, from: Data(json.utf8))
        #expect(document.path == "/tmp/memory.md")
        #expect(document.text == "hi")
    }

    // MARK: - JSONValue.displayText

    @Test func jsonValueDisplayText() {
        #expect(JSONValue.string("claude").displayText == "claude")
        #expect(JSONValue.number(1200).displayText == "1200")
        #expect(JSONValue.number(0.5).displayText == "0.5")
        #expect(JSONValue.bool(true).displayText == "On")
        #expect(JSONValue.bool(false).displayText == "Off")
        #expect(JSONValue.array([.string("claude"), .string("local")]).displayText == "claude, local")
        #expect(JSONValue.null.displayText == "")
    }

    // MARK: - PullEvent.parse

    @Test func pullEventParsesEveryEventInTheFixture() {
        let events = PreviewData.pullLines.compactMap(PullEvent.parse)
        #expect(events.count == PreviewData.pullLines.count)
        #expect(events.filter { if case .progress = $0 { return true }; return false }.count == 3)
        #expect(events.last == .done(path: "/Users/you/.cache/keryx/models/"
            + "unsloth--Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"))
    }

    @Test func pullEventParsesEveryEventKind() {
        #expect(PullEvent.parse(#"{"event": "progress", "done": 10, "total": 100}"#)
            == .progress(done: 10, total: 100))
        #expect(PullEvent.parse(#"{"event": "done", "path": "/x"}"#) == .done(path: "/x"))
        #expect(PullEvent.parse(#"{"event": "adopted", "path": "/x", "from": "/y"}"#)
            == .adopted(path: "/x", from: "/y"))
        #expect(PullEvent.parse(#"{"event": "error", "message": "disk full"}"#) == .error("disk full"))
        #expect(PullEvent.parse(#"{"event": "cancelled"}"#) == .cancelled)
    }

    @Test func pullEventParseIsNilForGarbage() {
        #expect(PullEvent.parse("not json at all") == nil)
        #expect(PullEvent.parse("") == nil)
        #expect(PullEvent.parse(#"{"event": "something-new"}"#) == nil)
        #expect(PullEvent.parse(#"{"no_event_key": true}"#) == nil)
    }

    // The contract fixture is checked, both ways, in ContractTests.
}

private func isoDate(_ string: String) -> Date {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: string)!
}
