import AppKit
import CoreFoundation
import Foundation
import SwiftUI
import Testing
@testable import KeryxKit
import KeryxPreview
import KeryxTestSupport
@testable import KeryxUI

/// Switching a Configuration tab, a sidebar section, or a programmatic `AppModel.selection`
/// change tears a field's row down in the same update as the change, before a focus-loss
/// commit would have run — so what was typed never reaches the CLI (#settings-save-bug). The
/// fix is a save on `.onDisappear` beside every focus-loss save; these tests hold that
/// structurally (every KeryxUI source file) and behaviourally (a real, hosted window).
@Suite struct ConfigSaveOnDisappearSourceTests {
    static func keryxUIFiles() throws -> [URL] {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/KeryxUI")
        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil) else {
            return []
        }
        return enumerator.compactMap { $0 as? URL }.filter { $0.pathExtension == "swift" }
    }

    /// Every place KeryxUI commits a text field when it loses focus (`.onChange(of:
    /// focused)`) has an `.onDisappear` commit close by it — found by source, since a
    /// hosted-window test can only drive the fields it already knows to look for.
    @Test func everyFocusLossCommitHasADisappearCommitBesideIt() throws {
        var found = 0
        for file in try Self.keryxUIFiles() {
            let text = try String(contentsOf: file, encoding: .utf8)
            var tail = text[...]
            while let range = tail.range(of: ".onChange(of: focused)") {
                found += 1
                let after = tail[range.upperBound...]
                #expect(after.prefix(500).contains(".onDisappear"),
                        "\(file.lastPathComponent) commits on focus loss with no .onDisappear beside it")
                tail = tail[range.upperBound...]
            }
        }
        // Today: ConfigRowView.field and PluginFieldRow.CommittingField. A third site should
        // extend this count, not slip past the loop above unseen.
        #expect(found == 2)
    }
}

/// A real (offscreen) window and AppModel, driven as a person's typing and a tab switch
/// would: the behavioural half of the fix.
@MainActor @Suite struct ConfigSaveOnDisappearUITests {
    /// `AppModel` wired to a real `CLIClient` over a `FakeRunner`, so a save shows up as the
    /// CLI invocation it really is (`config set KEY VALUE`), not a test double's own words
    /// for it.
    static func makeModel() -> (AppModel, FakeRunner) {
        let fake = FakeRunner { request in
            let args = request.arguments
            if args.suffix(2) == ["status", "--json"] {
                return .init(exitCode: 0, stdout: PreviewData.data("status.json"), stderr: Data())
            }
            if args.suffix(2) == ["doctor", "--json"] {
                return .init(exitCode: 0, stdout: PreviewData.data("doctor.json"), stderr: Data())
            }
            if args.suffix(3) == ["config", "list", "--json"] {
                return .init(exitCode: 0, stdout: PreviewData.data("config.json"), stderr: Data())
            }
            return .init(exitCode: 0, stdout: Data(), stderr: Data())
        }
        let model = AppModel(locator: PreviewLocator(), makeClient: { CLIClient(location: $0, runner: fake) },
                             preferences: Preferences(defaults: TestDefaults.fresh()), timing: AppModel.Timing(poll: nil))
        return (model, fake)
    }

    /// `view` in a real, offscreen window: a renderer draws pixels, but only an actual
    /// window gives a field a first responder to type into.
    static func host(_ view: some View) -> NSHostingView<AnyView> {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: NSRect(x: -10_000, y: 0, width: 900, height: 600),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let host = NSHostingView(rootView: AnyView(view))
        host.frame = NSRect(x: 0, y: 0, width: 900, height: 600)
        window.contentView = host
        window.orderFrontRegardless()
        return host
    }

    /// Lets the SwiftUI update cycle (a `.task`, an `.onChange`, a view torn down) catch up
    /// with a change made straight to the model.
    /// `CFRunLoopRunInMode` is unavailable to call directly from an async function; this
    /// plain, synchronous wrapper is the call site it sees instead.
    static func spinRunLoopOnce(_ seconds: CFTimeInterval) {
        CFRunLoopRunInMode(.defaultMode, seconds, false)
    }

    /// Lets SwiftUI catch up until `condition` holds — the run loop turned between looks,
    /// since a hosted window's updates are driven by it — rather than for a fixed time: the
    /// main actor is shared with every suite that renders views, and a busy one is slow.
    static func until(timeout: Duration = .seconds(180), _ condition: () -> Bool) async -> Bool {
        let clock = ContinuousClock()
        let deadline = clock.now + timeout
        while clock.now < deadline {
            if condition() { return true }
            await Task.yield()
            Self.spinRunLoopOnce(0.01)
        }
        return condition()
    }

    static func firstEditableTextField(in view: NSView) -> NSTextField? {
        if let field = view as? NSTextField, field.isEditable, field.isEnabled { return field }
        for sub in view.subviews {
            if let found = firstEditableTextField(in: sub) { return found }
        }
        return nil
    }

    /// Types into the first text field on Configuration's General tab (Owner name, unset,
    /// so the field editor is otherwise empty) and makes it first responder.
    static func typeIntoOwnerName(in host: NSView, _ text: String) throws -> NSTextField {
        let field = try #require(Self.firstEditableTextField(in: host), "no editable text field found")
        _ = host.window?.makeFirstResponder(field)
        let editor = try #require(field.currentEditor() as? NSTextView, "no field editor")
        editor.insertText(text, replacementRange: NSRange(location: 0, length: 0))
        return field
    }

    static func saved(_ fake: FakeRunner) -> Bool {
        fake.requests.contains { $0.arguments.suffix(4) == ["config", "set", "OWNER_NAME", "Ada"] }
    }

    @Test func typingThenSwitchingConfigurationTabsStillSaves() async throws {
        let (model, fake) = Self.makeModel()
        await model.start()
        let host = Self.host(ConfigurationView().environment(model))
        #expect(await Self.until { model.config.value != nil && Self.firstEditableTextField(in: host) != nil },
                "the General tab's fields")

        _ = try Self.typeIntoOwnerName(in: host, "Ada")
        model.request(.configure(tab: "voice"))

        #expect(await Self.until { Self.saved(fake) },
               "expected a config set for the field typed into before the tab switched")
    }

    @Test func typingThenAProgrammaticSelectionChangeStillSaves() async throws {
        let (model, fake) = Self.makeModel()
        await model.start()
        model.selection = .configuration
        let host = Self.host(RootView().environment(model))
        #expect(await Self.until { model.config.value != nil && Self.firstEditableTextField(in: host) != nil },
                "the General tab's fields")

        _ = try Self.typeIntoOwnerName(in: host, "Ada")
        // As the menu bar's Open … would: no AppRequest, just the selection itself.
        model.selection = .voice

        #expect(await Self.until { Self.saved(fake) },
               "expected a config set for the field typed into before the section changed")
    }
}
