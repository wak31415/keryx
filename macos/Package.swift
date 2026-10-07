// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "KeryxMac",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "KeryxApp", targets: ["KeryxApp"])],
    targets: [
        .target(name: "KeryxKit"),
        .target(name: "KeryxUI", dependencies: ["KeryxKit"]),
        // The app links only the two above. What previews, snapshots and tests draw from —
        // the sample documents, a client that answers from them, the screen catalog — is
        // here, and the test doubles in KeryxTestSupport, so none of it ships.
        .target(name: "KeryxPreview", dependencies: ["KeryxKit", "KeryxUI"], resources: [.copy("Preview")]),
        .target(name: "KeryxTestSupport", dependencies: ["KeryxKit"]),
        .executableTarget(name: "KeryxApp", dependencies: ["KeryxKit", "KeryxUI"]),
        .executableTarget(name: "KeryxSnapshots", dependencies: ["KeryxKit", "KeryxUI", "KeryxPreview"]),
        .testTarget(name: "KeryxKitTests", dependencies: ["KeryxKit", "KeryxPreview", "KeryxTestSupport"],
                    resources: [.copy("Fixtures")]),
        .testTarget(name: "KeryxUITests", dependencies: ["KeryxKit", "KeryxUI", "KeryxPreview", "KeryxTestSupport"]),
    ],
    swiftLanguageModes: [.v6]
)
