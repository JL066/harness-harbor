// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "HarnessHarbor",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "HarnessHarbor", targets: ["HarnessHarbor"])],
    targets: [
        .executableTarget(name: "HarnessHarbor", path: "Sources/HarnessHarbor"),
        .testTarget(name: "HarnessHarborTests", dependencies: ["HarnessHarbor"], path: "Tests/HarnessHarborTests")
    ]
)
