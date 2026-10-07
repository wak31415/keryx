// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "KeryxMac",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "KeryxApp", targets: ["KeryxApp"])],
    targets: [
        .target(name: "KeryxKit", resources: [.copy("Preview")]),
        .target(name: "KeryxUI", dependencies: ["KeryxKit"]),
        .executableTarget(name: "KeryxApp", dependencies: ["KeryxKit", "KeryxUI"]),
        .executableTarget(name: "KeryxSnapshots", dependencies: ["KeryxKit", "KeryxUI"]),
        .testTarget(name: "KeryxKitTests", dependencies: ["KeryxKit"],
                    resources: [.copy("Fixtures")]),
        .testTarget(name: "KeryxUITests", dependencies: ["KeryxKit", "KeryxUI"]),
    ],
    swiftLanguageModes: [.v6]
)
