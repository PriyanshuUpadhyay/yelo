// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "UsageHUD",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "UsageHUD", path: "Sources/UsageHUD"),
        .testTarget(name: "UsageHUDTests", dependencies: ["UsageHUD"], path: "Tests/UsageHUDTests"),
    ]
)
