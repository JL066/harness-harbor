#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
mkdir -p build/swift-checks
if [ "$#" -gt 0 ]; then
    export HARBOR_RUNTIME_EXE="$1"
    export HARBOR_STATE_DIR="$PWD/build/swift-checks/state"
    export HARBOR_USER_SETTINGS_DIR="$PWD/build/swift-checks/settings"
    export HARBOR_LOG_DIR="$PWD/build/swift-checks/logs"
    export HARBOR_TUNNEL_PROFILE_DIR="$PWD/build/swift-checks/tunnel"
fi
swiftc -parse-as-library macos/Sources/HarnessHarbor/Model.swift macos/Sources/HarnessHarbor/Bridge.swift macos/Sources/HarnessHarbor/Keychain.swift macos/Checks.swift -o build/swift-checks/check
build/swift-checks/check "$PWD/build/swift-checks"
