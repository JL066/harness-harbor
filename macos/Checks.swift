import Foundation
import Security
import LocalAuthentication

// Runnable without full Xcode/XCTest; CI additionally runs the XCTest suite.
@main struct HarborChecks {
    static func rejects(_ operation: () throws -> Void) {
        do { try operation(); preconditionFailure("Invalid input was accepted") } catch { }
    }
    static func main() async throws {
        var presenceQueries = 0
        let present = try HarborKeychain.contains(HarborCredentialTarget.tunnel) { query, _ in
            presenceQueries += 1
            let fields = query as NSDictionary
            precondition(fields[kSecReturnData] == nil)
            precondition(fields[kSecReturnAttributes] as? Bool == true)
            precondition((fields[kSecUseAuthenticationContext] as? LAContext)?.interactionNotAllowed == true)
            precondition(fields[kSecAttrService] as? String == HarborKeychain.service)
            precondition(fields[kSecAttrAccount] as? String == HarborCredentialTarget.tunnel)
            return errSecSuccess
        }
        precondition(present && presenceQueries == 1)
        let absent = try HarborKeychain.contains(HarborCredentialTarget.codexCustom) { _, _ in errSecItemNotFound }
        precondition(!absent)
        rejects { _ = try HarborKeychain.contains(HarborCredentialTarget.tunnel) { _, _ in errSecInteractionNotAllowed } }
        rejects { _ = try HarborKeychain.contains("unrelated") { _, _ in preconditionFailure("Invalid target queried") } }

        try SettingsValidator.validateURL("https://api.example.test/v1")
        for raw in [#"{}"#, #"{"base_url":""}"#, #"{"base_url":"  "}"#] {
            let connection = try JSONDecoder().decode(ConnectionSettings.self, from: Data(raw.utf8))
            precondition(connection.baseURL == "https://api.openai.com")
            precondition(!connection.requiresCredential)
        }
        precondition(ConnectionSettings().baseURL == "https://api.openai.com")
        let customConnection = try JSONDecoder().decode(ConnectionSettings.self, from: Data(#"{"base_url":"https://tunnel.example.test"}"#.utf8))
        precondition(customConnection.baseURL == "https://tunnel.example.test")
        precondition(ConnectionSettings(tunnelID: "tunnel-test").requiresCredential)
        rejects { try SettingsValidator.validateURL("https://user:secret@example.test") }
        rejects { try SettingsValidator.validateURL("http://example.test") }
        rejects { try SettingsValidator.validateURL("https://example.test/?token=secret") }
        precondition(!SettingsValidator.isSafeProfile(String(repeating: "a", count: 81)))
        precondition(JSONValue.number(1e100).intValue == nil)
        let activeHarness = try JSONDecoder().decode(HarborHarness.self, from: Data(#"{"name":"codex","summary":"telemetry stale","running_jobs":2,"running_job_ids":["job-1234","job-5678"],"activity_fresh":true}"#.utf8))
        precondition(activeHarness.runningJobsLabel == "2 running · 1234, 5678")
        let staleHarness = try JSONDecoder().decode(HarborHarness.self, from: Data(#"{"name":"codex","running_jobs":2,"running_job_ids":["job-1234"],"activity_fresh":false}"#.utf8))
        precondition(staleHarness.runningJobsLabel == nil)
        let missingFreshHarness = try JSONDecoder().decode(HarborHarness.self, from: Data(#"{"name":"codex","running_jobs":2,"running_job_ids":["job-1234"]}"#.utf8))
        precondition(missingFreshHarness.runningJobsLabel == nil)
        let legacyHarness = try JSONDecoder().decode(HarborHarness.self, from: Data(#"{"name":"codex","running_jobs":2}"#.utf8))
        precondition(legacyHarness.runningJobsLabel == nil)
        let idleHarness = try JSONDecoder().decode(HarborHarness.self, from: Data(#"{"name":"codex","running_jobs":0,"running_job_ids":["job-1234"],"activity_fresh":true}"#.utf8))
        precondition(idleHarness.runningJobsLabel == nil)
        let legacy = Data(#"{"executables":{"minimax":"/Applications/MiniMax.app/Contents/MacOS/MiniMax","mcode":"/opt/bin/mcode"}}"#.utf8)
        let migrated = try JSONDecoder().decode(MacSettings.self, from: legacy)
        precondition(migrated.executables["minimax"] == "/opt/bin/mcode")
        precondition(migrated.executables["mcode"] == nil)
        precondition(MacSettings.executableKeys == ["codex", "agy", "minimax", "tunnel"])
        var desktop = HarborSettings()
        desktop.macos.executables["minimax"] = "/Applications/MiniMax.app/Contents/MacOS/MiniMax"
        rejects { try SettingsValidator.validate(desktop, checkExecutables: false) }
        rejects { _ = try BridgeProtocol.makeRequest(method: "shell.exec") }
        rejects { _ = try BridgeProtocol.makeRequest(method: "runtime.start", params: ["argv": .array([])]) }
        rejects { _ = try BridgeProtocol.makeRequest(method: "logs.tail", params: ["component": .string("../settings"), "lines": .number(10)]) }
        let request = try BridgeProtocol.makeRequest(method: "hello")
        let data = try BridgeProtocol.encodeRequest(request)
        precondition(data.last == 10)
        let good = Data("{\"v\":1,\"id\":\"\(request.id)\",\"ok\":true,\"result\":{}}".utf8)
        let response = try BridgeProtocol.decodeResponse(good)
        precondition(response.ok)
        rejects { _ = try BridgeProtocol.decodeResponse(Data("{\"v\":9}".utf8)) }
        let validationParams: [String: JSONValue] = [
            "settings": .object([:]),
            "credentials": .object(["tunnel": .bool(true), "custom": .bool(false)]),
            "require_connection": .bool(true)
        ]
        let validationRequest = try BridgeProtocol.makeRequest(method: "settings.validate", params: validationParams)
        precondition(Set(validationRequest.params.keys) == Set(["settings", "credentials", "require_connection"]))
        rejects { _ = try BridgeProtocol.makeRequest(method: "settings.validate", params: ["settings": .object([:]), "credentials": validationParams["credentials"]!]) }
        rejects { _ = try BridgeProtocol.makeRequest(method: "settings.validate", params: validationParams.merging(["extra": .bool(true)]) { _, new in new }) }
        let validationResult = HarborSettingsValidation(ok: false, errors: ["Tunnel ID is required."])
        let validationValue = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(validationResult))
        let decodedValidation = try validationValue.decoded(HarborSettingsValidation.self)
        precondition(decodedValidation.errors == ["Tunnel ID is required."])
        let path = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("settings.json")
        let settings = HarborSettings()
        try SettingsStore.save(settings, to: path)
        let created = try SettingsStore.load(from: path)
        precondition(created == settings)
        try SettingsStore.save(settings, to: path)
        let replaced = try SettingsStore.load(from: path)
        precondition(replaced == settings)
        let unknownPath = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("unknown-settings.json")
        let unknownDocument: [String: Any] = [
            "version": 1,
            "future_root": ["enabled": true],
            "connection": ["future_connection": ["mode": "managed"]],
            "codex": ["future_codex": ["region": "test"], "custom": ["future_custom": ["priority": 7]]],
            "macos": ["future_macos": ["appearance": "dark"]],
            "windows": ["future_windows": ["shell": "powershell"]]
        ]
        try JSONSerialization.data(withJSONObject: unknownDocument).write(to: unknownPath)
        var unknownSettings = try SettingsStore.load(from: unknownPath)
        unknownSettings.connection.tunnelID = "tunnel-2"
        try SettingsStore.save(unknownSettings, to: unknownPath)
        let savedUnknown = try JSONSerialization.jsonObject(with: Data(contentsOf: unknownPath)) as! [String: Any]
        precondition((savedUnknown["future_root"] as? [String: Any])?["enabled"] as? Bool == true)
        precondition(((savedUnknown["connection"] as? [String: Any])?["future_connection"] as? [String: Any])?["mode"] as? String == "managed")
        precondition(((savedUnknown["codex"] as? [String: Any])?["future_codex"] as? [String: Any])?["region"] as? String == "test")
        precondition(((((savedUnknown["codex"] as? [String: Any])?["custom"] as? [String: Any])?["future_custom"] as? [String: Any])?["priority"] as? Int) == 7)
        precondition(((savedUnknown["macos"] as? [String: Any])?["future_macos"] as? [String: Any])?["appearance"] as? String == "dark")
        precondition(((savedUnknown["windows"] as? [String: Any])?["future_windows"] as? [String: Any])?["shell"] as? String == "powershell")
        precondition((savedUnknown["connection"] as? [String: Any])?["tunnel_id"] as? String == "tunnel-2")
        print("Swift checks passed: URL/path validation, protocol rejection/bounds, settings create/replace round trip.")
        if ProcessInfo.processInfo.environment["HARBOR_RUNTIME_EXE"] != nil {
            let bridge = HarborBridge()
            // Reusing an already-connected bridge must still complete its callback.
            for _ in 0..<2 {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                    bridge.start { continuation.resume(with: $0.mapError { $0 as Error }) }
                }
            }
            let diagnostics: [String: JSONValue] = try await withCheckedThrowingContinuation { continuation in
                bridge.diagnostics { continuation.resume(with: $0.mapError { $0 as Error }) }
            }
            precondition(Set(diagnostics["detected_executables"]?.objectValue?.keys.map { $0 } ?? []) == Set(MacSettings.executableKeys))
            let snapshot: HarborStatusSnapshot = try await withCheckedThrowingContinuation { continuation in
                bridge.startRuntime { continuation.resume(with: $0.mapError { $0 as Error }) }
            }
            precondition(snapshot.components["mcp"]?.state == "healthy")
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                bridge.shutdown { continuation.resume(with: $0.mapError { $0 as Error }) }
            }
            print("Swift live bridge checks passed: repeated connect, runtime start, shutdown.")
        }
    }
}
