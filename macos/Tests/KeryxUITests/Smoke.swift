import Testing
@testable import KeryxUI

@Suite struct SmokeTests {
    @Test func keryxUITargetCompiles() {
        let _: KeryxUI.Type = KeryxUI.self
    }
}
