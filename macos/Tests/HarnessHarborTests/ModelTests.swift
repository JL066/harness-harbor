import Foundation
import Security
import LocalAuthentication
import XCTest
@testable import HarnessHarbor

final class ModelTests: XCTestCase {
    func testCredentialPresenceNeverRequestsSecretsOrAuthorizationUI() throws {
        let exists = try HarborKeychain.contains(HarborCredentialTarget.tunnel) { query, _ in
            let fields = query as NSDictionary
            XCTAssertNil(fields[kSecReturnData])
            XCTAssertEqual(fields[kSecReturnAttributes] as? Bool, true)
            XCTAssertEqual((fields[kSecUseAuthenticationContext] as? LAContext)?.interactionNotAllowed, true)
            return errSecSuccess
        }
        XCTAssertTrue(exists)
        XCTAssertFalse(try HarborKeychain.contains(HarborCredentialTarget.tunnel) { _, _ in errSecItemNotFound })
        XCTAssertThrowsError(try HarborKeychain.contains(HarborCredentialTarget.tunnel) { _, _ in errSecInteractionNotAllowed })
    }

    func testSettingsSchemaContainsOnlyNonSecretFields() throws {
        let data = try JSONEncoder().encode(HarborSettings())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["version"] as? Int, 1)
        XCTAssertEqual((object["connection"] as? [String: Any])?["credential_ref"] as? String, HarborCredentialTarget.tunnel)
        let codex = object["codex"] as? [String: Any]
        let custom = codex?["custom"] as? [String: Any]
        XCTAssertEqual(custom?["credential_ref"] as? String, HarborCredentialTarget.codexCustom)
        XCTAssertNil(custom?["api_key"])
        XCTAssertNotNil(object["macos"])
    }

    func testURLAndProfileValidation() throws {
        try SettingsValidator.validateURL("https://api.example.test/v1")
        try SettingsValidator.validateURL("http://127.0.0.1:8765/mcp")
        XCTAssertThrowsError(try SettingsValidator.validateURL("https://user:pass@example.test"))
        XCTAssertThrowsError(try SettingsValidator.validateURL("http://example.test"))
        XCTAssertThrowsError(try SettingsValidator.validateURL("https://example.test/?debug=1"))
        XCTAssertTrue(SettingsValidator.isSafeProfile("harbor-1.prod"))
        XCTAssertFalse(SettingsValidator.isSafeProfile(String(repeating: "a", count: 81)))

        var custom = HarborSettings(codex: CodexSettings(routingMode: HarborRoute.custom.rawValue, custom: CodexCustomSettings(enabled: true, baseURL: "https://api.example.test/v1")))
        try SettingsValidator.validate(custom)
        custom.connection.profileName = "bad/name"
        XCTAssertThrowsError(try SettingsValidator.validate(custom))
    }

    func testAtomicSettingsRoundTripAndExecutableValidation() throws {
        let target = FileManager.default.temporaryDirectory.appendingPathComponent("HarnessHarbor-\(UUID().uuidString)/settings.json")
        defer { try? FileManager.default.removeItem(at: target.deletingLastPathComponent()) }
        var settings = HarborSettings(connection: ConnectionSettings(tunnelID: "tunnel-1", baseURL: "https://control.example.test", profileName: "prod"))
        settings.macos.executables["codex"] = "/bin/sh"
        try SettingsStore.save(settings, to: target, requireConnection: true)
        XCTAssertEqual(try SettingsStore.load(from: target), settings)
        settings.macos.executables["codex"] = "relative/codex"
        XCTAssertThrowsError(try SettingsStore.save(settings, to: target))
        XCTAssertEqual(try SettingsStore.load(from: target).connection.tunnelID, "tunnel-1")
    }

    func testSettingsSavePreservesUnknownFieldsAcrossAllSections() throws {
        let target = FileManager.default.temporaryDirectory.appendingPathComponent("HarnessHarbor-\(UUID().uuidString)/settings.json")
        defer { try? FileManager.default.removeItem(at: target.deletingLastPathComponent()) }
        let raw: [String: Any] = [
            "version": 1,
            "future_root": ["enabled": true],
            "connection": [
                "tunnel_id": "tunnel-1",
                "base_url": "https://control.example.test",
                "profile_name": "prod",
                "credential_ref": HarborCredentialTarget.tunnel,
                "future_connection": ["mode": "managed"]
            ],
            "codex": [
                "routing_mode": "current",
                "future_codex": ["region": "test"],
                "custom": [
                    "enabled": false,
                    "profile_name": "",
                    "base_url": "",
                    "default_model": "",
                    "credential_ref": HarborCredentialTarget.codexCustom,
                    "future_custom": ["priority": 7]
                ]
            ],
            "macos": [
                "start_at_launch": false,
                "open_dashboard": false,
                "setup_complete": false,
                "executables": ["codex": ""],
                "future_macos": ["appearance": "dark"]
            ],
            "windows": ["future_windows": ["shell": "powershell"]]
        ]
        let data = try JSONSerialization.data(withJSONObject: raw)
        try FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: target)

        var settings = try SettingsStore.load(from: target)
        settings.connection.tunnelID = "tunnel-2"
        try SettingsStore.save(settings, to: target)

        let saved = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: target)) as? [String: Any])
        XCTAssertEqual((saved["future_root"] as? [String: Any])?["enabled"] as? Bool, true)
        let savedConnection = saved["connection"] as? [String: Any]
        let savedCodex = saved["codex"] as? [String: Any]
        let savedCustom = savedCodex?["custom"] as? [String: Any]
        let savedMacOS = saved["macos"] as? [String: Any]
        let savedWindows = saved["windows"] as? [String: Any]
        XCTAssertEqual((savedConnection?["future_connection"] as? [String: Any])?["mode"] as? String, "managed")
        XCTAssertEqual((savedCodex?["future_codex"] as? [String: Any])?["region"] as? String, "test")
        XCTAssertEqual((savedCustom?["future_custom"] as? [String: Any])?["priority"] as? Int, 7)
        XCTAssertEqual((savedMacOS?["future_macos"] as? [String: Any])?["appearance"] as? String, "dark")
        XCTAssertEqual((savedWindows?["future_windows"] as? [String: Any])?["shell"] as? String, "powershell")
        XCTAssertEqual((saved["connection"] as? [String: Any])?["tunnel_id"] as? String, "tunnel-2")
    }

    func testSettingsValidationProtocolContract() throws {
        let credentials: JSONValue = .object(["tunnel": .bool(true), "custom": .bool(false)])
        let params: [String: JSONValue] = [
            "settings": .object([:]),
            "credentials": credentials,
            "require_connection": .bool(true)
        ]
        let request = try BridgeProtocol.makeRequest(method: "settings.validate", params: params)
        XCTAssertEqual(Set(request.params.keys), Set(["settings", "credentials", "require_connection"]))
        XCTAssertEqual(try JSONDecoder().decode(BridgeRequest.self, from: Data(try BridgeProtocol.encodeRequest(request).dropLast())).method, "settings.validate")
        XCTAssertThrowsError(try BridgeProtocol.makeRequest(method: "settings.validate", params: ["settings": .object([:]), "credentials": credentials]))
        XCTAssertThrowsError(try BridgeProtocol.makeRequest(method: "settings.validate", params: ["settings": .object([:]), "credentials": credentials, "require_connection": .bool(false), "extra": .bool(true)]))

        let result = HarborSettingsValidation(ok: false, errors: ["Tunnel ID is required."])
        let response = BridgeResponse(id: UUID().uuidString, ok: true, result: try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(result)))
        let decoded = try XCTUnwrap(try BridgeProtocol.decodeResponse(JSONEncoder().encode(response)).result?.decoded(HarborSettingsValidation.self))
        XCTAssertEqual(decoded.errors, ["Tunnel ID is required."])
    }

    func testNDJSONProtocolRules() throws {
        let request = try BridgeProtocol.makeRequest(method: "logs.tail", params: ["component": .string("runtime"), "lines": .number(20)])
        let line = try BridgeProtocol.encodeRequest(request)
        XCTAssertEqual(line.last, 10)
        XCTAssertEqual(try JSONDecoder().decode(BridgeRequest.self, from: Data(line.dropLast())).method, "logs.tail")
        XCTAssertThrowsError(try BridgeProtocol.makeRequest(method: "logs.tail", params: ["component": .string("runtime"), "lines": .number(201)]))

        let id = UUID().uuidString
        let response = BridgeResponse(id: id, ok: true, result: .object(["ok": .bool(true)]))
        let decoded = try BridgeProtocol.decodeResponse(JSONEncoder().encode(response))
        XCTAssertEqual(decoded.id, id)
        XCTAssertThrowsError(try BridgeProtocol.decodeResponse(Data(#"{"v":1,"id":"\#(id)","ok":true}"#.utf8)))
    }
}
