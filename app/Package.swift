// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Framescribe",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Framescribe", targets: ["Framescribe"]),
        .executable(name: "framescribe-e2e", targets: ["FramescribeE2E"]),
        .library(name: "FramescribeCore", targets: ["FramescribeCore"]),
    ],
    targets: [
        // Everything that can be tested without a window: events, job state, the queue, tool location, paths.
        .target(name: "FramescribeCore"),
        // The SwiftUI app.
        .executableTarget(name: "Framescribe", dependencies: ["FramescribeCore"]),
        // A command line driver that runs a built app's embedded tool through the same library code.
        .executableTarget(name: "FramescribeE2E", dependencies: ["FramescribeCore"]),
        .testTarget(name: "FramescribeCoreTests", dependencies: ["FramescribeCore"],
                    resources: [.copy("Fixtures")]),
    ]
)
