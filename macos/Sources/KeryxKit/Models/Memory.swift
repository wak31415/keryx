import Foundation

/// `keryx memory --json`: `text` is `nil` when the file does not exist yet (no call has
/// ever been authorized), or when it exists but could not be read.
public struct MemoryDocument: Codable, Sendable, Equatable {
    public var path: String
    public var exists: Bool
    public var text: String?
}
