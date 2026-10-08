import Testing
@testable import KeryxKit
import KeryxPreview
@testable import KeryxUI

/// The secret sheet's check before saving (`config verify KEY --stdin`).
@Suite struct SecretCheckTests {
    @Test func onlyTheKeysKeryxCanCheckAreChecked() {
        #expect(SecretCheck.applies(to: "OPENAI_API_KEY"))
        #expect(SecretCheck.applies(to: "ANTHROPIC_API_KEY"))
        #expect(SecretCheck.applies(to: "CODEX_API_KEY"))
        #expect(!SecretCheck.applies(to: "TWILIO_AUTH_TOKEN"), "checked with its SID, in the Phone section")
        #expect(!SecretCheck.applies(to: "SLACK_BOT_TOKEN"))
        #expect(!SecretCheck.applies(to: "VOICE_API_KEY"))
        #expect(SecretCheck.keys.isSubset(of: VerifyResult.verifiable))
    }

    @Test func saveChecksFirstAndAProblemMakesItSaveAnyway() {
        #expect(SecretCheck.unchecked.mustCheckFirst)
        #expect(!SecretCheck.works(detail: nil).mustCheckFirst)
        #expect(!SecretCheck.problem("No.").mustCheckFirst)
        #expect(SecretCheck.unchecked.saveTitle == "Save")
        #expect(SecretCheck.works(detail: nil).saveTitle == "Save")
        #expect(SecretCheck.problem("No.").saveTitle == "Save Anyway")
        #expect(SecretCheck.problem("No.").savesAnyway, "never on Return")
        #expect(!SecretCheck.works(detail: nil).savesAnyway)
        #expect(!SecretCheck.unchecked.savesAnyway)
    }

    @Test func anAnswerBecomesAState() {
        #expect(SecretCheck(.success(PreviewClient.verified)) == .works(detail: nil))
        #expect(SecretCheck(.success(PreviewClient.notVerified)) == .problem("OpenAI didn't accept the key (401)."))
        #expect(SecretCheck(.failure(.failed(command: "keryx config verify", exitCode: 2, message: "no check for that key")))
            == .problem("Keryx couldn't check the key."), "what Keryx said is not the sentence")
        #expect(SecretCheck(.failure(.failed(command: "keryx config verify", exitCode: 2, message: "No such command 'verify'.")))
            == .problem(KeryxError.olderKeryx))
        #expect(SecretCheck(.failure(.timedOut(command: "keryx config verify")))
            == .problem(KeryxError.timedOut(command: "keryx config verify").message))
    }

    @Test func itSaysWhoAcceptedTheKey() {
        #expect(SecretCheck.works(detail: nil).said(key: "ANTHROPIC_API_KEY")?.text == "Anthropic accepted the key.")
        #expect(SecretCheck.works(detail: nil).said(key: "CODEX_API_KEY")?.text == "OpenAI accepted the key.")
        #expect(SecretCheck.problem("No.").said(key: "OPENAI_API_KEY")?.works == false)
        #expect(SecretCheck.checking.said(key: "OPENAI_API_KEY") == nil)
    }
}
