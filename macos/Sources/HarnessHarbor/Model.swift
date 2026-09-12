import Combine
import Foundation
import AppKit
import ServiceManagement

public enum HarborRoute: String, CaseIterable, Identifiable {
    case current
    case official
    case custom
    case officialThenCustom = "official_then_custom"

    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .current: return "Current Codex configuration"
        case .official: return "Official OpenAI"
        case .custom: return "Custom OpenAI-compatible provider"
        case .officialThenCustom: return "Official, then custom on quota exhaustion"
        }
    }
}

public enum HarborCredentialTarget {
    public static let tunnel = "Harness-Harbor:tunnel:runtime_key"
    public static let codexCustom = "Harness-Harbor:codex:custom_api_key"
}

public struct ConnectionSettings: Codable, Equatable, Sendable {
    public static let defaultBaseURL = "https://api.openai.com"
    public var tunnelID: String
    public var baseURL: String
    public var profileName: String
    public var credentialRef: String

    public init(tunnelID: String = "", baseURL: String = ConnectionSettings.defaultBaseURL, profileName: String = "harness-harbor", credentialRef: String = HarborCredentialTarget.tunnel) {
        self.tunnelID = tunnelID
        self.baseURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Self.defaultBaseURL : baseURL
        self.profileName = profileName
        self.credentialRef = credentialRef
    }

    enum CodingKeys: String, CodingKey { case tunnelID = "tunnel_id", baseURL = "base_url", profileName = "profile_name", credentialRef = "credential_ref" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        tunnelID = try c.decodeIfPresent(String.self, forKey: .tunnelID) ?? ""
        let savedURL = try c.decodeIfPresent(String.self, forKey: .baseURL) ?? ""
        baseURL = savedURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Self.defaultBaseURL : savedURL
        profileName = try c.decodeIfPresent(String.self, forKey: .profileName) ?? "harness-harbor"
        credentialRef = try c.decodeIfPresent(String.self, forKey: .credentialRef) ?? HarborCredentialTarget.tunnel
    }

    public var requiresCredential: Bool {
        !tunnelID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
        (!baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && baseURL != Self.defaultBaseURL)
    }
}

public struct CodexCustomSettings: Codable, Equatable, Sendable {
    public var enabled: Bool
    public var profileName: String
    public var baseURL: String
    public var defaultModel: String
    public var credentialRef: String

    public init(enabled: Bool = false, profileName: String = "", baseURL: String = "", defaultModel: String = "", credentialRef: String = HarborCredentialTarget.codexCustom) {
        self.enabled = enabled
        self.profileName = profileName
        self.baseURL = baseURL
        self.defaultModel = defaultModel
        self.credentialRef = credentialRef
    }

    enum CodingKeys: String, CodingKey { case enabled, profileName = "profile_name", baseURL = "base_url", defaultModel = "default_model", credentialRef = "credential_ref" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
        profileName = try c.decodeIfPresent(String.self, forKey: .profileName) ?? ""
        baseURL = try c.decodeIfPresent(String.self, forKey: .baseURL) ?? ""
        defaultModel = try c.decodeIfPresent(String.self, forKey: .defaultModel) ?? ""
        credentialRef = try c.decodeIfPresent(String.self, forKey: .credentialRef) ?? HarborCredentialTarget.codexCustom
    }
}

public struct CodexSettings: Codable, Equatable, Sendable {
    public var routingMode: String
    public var custom: CodexCustomSettings

    public init(routingMode: String = HarborRoute.current.rawValue, custom: CodexCustomSettings = .init()) {
        self.routingMode = routingMode
        self.custom = custom
    }

    enum CodingKeys: String, CodingKey { case routingMode = "routing_mode", custom }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        routingMode = try c.decodeIfPresent(String.self, forKey: .routingMode) ?? HarborRoute.current.rawValue
        custom = try c.decodeIfPresent(CodexCustomSettings.self, forKey: .custom) ?? .init()
    }
}

public struct MacSettings: Codable, Equatable, Sendable {
    public var startAtLaunch: Bool
    public var openDashboard: Bool
    public var setupComplete: Bool
    public var executables: [String: String]

    public static let executableKeys = ["codex", "agy", "minimax", "tunnel"]
    public static let defaultExecutables = Dictionary(uniqueKeysWithValues: executableKeys.map { ($0, "") })

    private static func normalizedExecutables(_ supplied: [String: String]) -> [String: String] {
        var values = supplied
        if let legacy = values.removeValue(forKey: "mcode") { values["minimax"] = legacy }
        return defaultExecutables.merging(values) { _, new in new }
    }

    public init(startAtLaunch: Bool = false, openDashboard: Bool = false, setupComplete: Bool = false, executables: [String: String] = MacSettings.defaultExecutables) {
        self.startAtLaunch = startAtLaunch
        self.openDashboard = openDashboard
        self.setupComplete = setupComplete
        self.executables = MacSettings.normalizedExecutables(executables)
    }

    enum CodingKeys: String, CodingKey { case startAtLaunch = "start_at_launch", openDashboard = "open_dashboard", setupComplete = "setup_complete", executables }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        startAtLaunch = try c.decodeIfPresent(Bool.self, forKey: .startAtLaunch) ?? false
        openDashboard = try c.decodeIfPresent(Bool.self, forKey: .openDashboard) ?? false
        setupComplete = try c.decodeIfPresent(Bool.self, forKey: .setupComplete) ?? false
        let supplied = try c.decodeIfPresent([String: String].self, forKey: .executables) ?? [:]
        executables = MacSettings.normalizedExecutables(supplied)
    }
}

public struct HarborSettings: Codable, Equatable, Sendable {
    public var version: Int
    public var connection: ConnectionSettings
    public var codex: CodexSettings
    public var macos: MacSettings

    public init(version: Int = 1, connection: ConnectionSettings = .init(), codex: CodexSettings = .init(), macos: MacSettings = .init()) {
        self.version = version
        self.connection = connection
        self.codex = codex
        self.macos = macos
    }

    enum CodingKeys: String, CodingKey { case version, connection, codex, macos }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decodeIfPresent(Int.self, forKey: .version) ?? 1
        connection = try c.decodeIfPresent(ConnectionSettings.self, forKey: .connection) ?? .init()
        codex = try c.decodeIfPresent(CodexSettings.self, forKey: .codex) ?? .init()
        macos = try c.decodeIfPresent(MacSettings.self, forKey: .macos) ?? .init()
    }
}

public enum HarborSettingsError: Error, LocalizedError {
    case invalid(String)
    case unsupportedVersion(Int)
    case corrupt
    case secretKey
    case io

    public var errorDescription: String? {
        switch self {
        case .invalid(let message): return message
        case .unsupportedVersion(let version): return "Settings schema version \(version) is unsupported."
        case .corrupt: return "The existing settings file is invalid; it was left unchanged."
        case .secretKey: return "Settings contain a secret field; secrets must remain in Keychain."
        case .io: return "Settings could not be read or written; the existing file was left unchanged."
        }
    }
}

public enum SettingsValidator {
    public static let schemaVersion = 1

    public static func isSafeProfile(_ value: String) -> Bool {
        let bytes = Array(value.utf8)
        guard (1...80).contains(bytes.count), let first = bytes.first, isASCIIAlphaNumeric(first) else { return false }
        return bytes.allSatisfy { isASCIIAlphaNumeric($0) || $0 == 45 || $0 == 95 || $0 == 46 }
    }

    public static func validateURL(_ raw: String, label: String = "URL") throws {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: value), let scheme = components.scheme?.lowercased(), let host = components.host, !host.isEmpty else {
            throw HarborSettingsError.invalid("\(label) must be an HTTPS URL or loopback HTTP URL.")
        }
        guard components.user == nil, components.password == nil, components.query == nil, components.fragment == nil else {
            throw HarborSettingsError.invalid("\(label) may not contain userinfo, a query, or a fragment.")
        }
        if scheme == "https" { return }
        guard scheme == "http", ["localhost", "127.0.0.1", "::1"].contains(host.lowercased()) else {
            throw HarborSettingsError.invalid("\(label) must use HTTPS unless it targets localhost.")
        }
    }

    public static func validate(_ settings: HarborSettings, requireConnection: Bool = false, checkExecutables: Bool = true) throws {
        guard settings.version == schemaVersion else {
            if settings.version > schemaVersion { throw HarborSettingsError.unsupportedVersion(settings.version) }
            throw HarborSettingsError.invalid("Settings schema version must be 1.")
        }
        guard settings.connection.credentialRef == HarborCredentialTarget.tunnel, settings.codex.custom.credentialRef == HarborCredentialTarget.codexCustom else {
            throw HarborSettingsError.invalid("Settings use an unsupported credential reference.")
        }
        guard HarborRoute(rawValue: settings.codex.routingMode) != nil else {
            throw HarborSettingsError.invalid("Choose a supported Codex route.")
        }

        let connection = settings.connection
        if requireConnection && connection.tunnelID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw HarborSettingsError.invalid("Tunnel ID is required.")
        }
        if !connection.profileName.isEmpty && !isSafeProfile(connection.profileName) {
            throw HarborSettingsError.invalid("Tunnel profile name may contain only letters, numbers, '-', '_' and '.'.")
        }
        if !connection.baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            try validateURL(connection.baseURL, label: "Tunnel base URL")
        }

        let routeNeedsCustom = settings.codex.routingMode == HarborRoute.custom.rawValue || settings.codex.routingMode == HarborRoute.officialThenCustom.rawValue
        let custom = settings.codex.custom
        if routeNeedsCustom || custom.enabled {
            guard !custom.baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw HarborSettingsError.invalid("Custom provider base URL is required.")
            }
            try validateURL(custom.baseURL, label: "Custom provider base URL")
            if !custom.profileName.isEmpty && !isSafeProfile(custom.profileName) {
                throw HarborSettingsError.invalid("Custom provider profile name is invalid.")
            }
        } else if !custom.profileName.isEmpty {
            guard isSafeProfile(custom.profileName) else { throw HarborSettingsError.invalid("Custom provider profile name is invalid.") }
        } else if !custom.baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            try validateURL(custom.baseURL, label: "Custom provider base URL")
        }

        try validateExecutables(settings, checkExecutables: checkExecutables)
    }

    public static func validateExecutables(_ settings: HarborSettings, checkExecutables: Bool = true) throws {
        guard Set(settings.macos.executables.keys).isSubset(of: Set(MacSettings.executableKeys)) else {
            throw HarborSettingsError.invalid("Settings contain an unsupported executable key.")
        }
        for key in MacSettings.executableKeys {
            let path = settings.macos.executables[key] ?? ""
            if !path.isEmpty {
                if key == "minimax" && !["mcode", "mcode.cmd", "mcode.bat"].contains(URL(fileURLWithPath: path).lastPathComponent) {
                    throw HarborSettingsError.invalid("MiniMax requires the mcode CLI executable.")
                }
                guard path.hasPrefix("/"), !checkExecutables || FileManager.default.isExecutableFile(atPath: path) else {
                    throw HarborSettingsError.invalid("Configured \(key) executable must be an absolute executable path.")
                }
            }
        }
    }

    private static func isASCIIAlphaNumeric(_ byte: UInt8) -> Bool {
        (byte >= 48 && byte <= 57) || (byte >= 65 && byte <= 90) || (byte >= 97 && byte <= 122)
    }
}

public enum SettingsStore {
    public static func settingsDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment["HARBOR_USER_SETTINGS_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?.appendingPathComponent("Harness Harbor", isDirectory: true)
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/Harness Harbor", isDirectory: true)
    }

    public static func settingsURL() -> URL { settingsDirectory().appendingPathComponent("settings.json") }

    public static func load(from url: URL = SettingsStore.settingsURL()) throws -> HarborSettings {
        let target = url.standardizedFileURL
        guard FileManager.default.fileExists(atPath: target.path) else { return HarborSettings() }
        let data: Data
        do {
            let size = try target.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= 65536 else { throw HarborSettingsError.corrupt }
            data = try Data(contentsOf: target)
        } catch { throw HarborSettingsError.io }
        do {
            let object = try JSONSerialization.jsonObject(with: data)
            try rejectSecretKeys(object)
            let settings = try JSONDecoder().decode(HarborSettings.self, from: data)
            try SettingsValidator.validate(settings, checkExecutables: false)
            return settings
        } catch let error as HarborSettingsError {
            throw error
        } catch {
            throw HarborSettingsError.corrupt
        }
    }

    public static func save(_ settings: HarborSettings, to url: URL = SettingsStore.settingsURL(), requireConnection: Bool = false) throws {
        try write(settings, to: url, requireConnection: requireConnection, validateBusiness: true)
    }

    fileprivate static func saveAfterRuntimeValidation(_ settings: HarborSettings, to url: URL = SettingsStore.settingsURL(), requireConnection: Bool = false) throws {
        try write(settings, to: url, requireConnection: requireConnection, validateBusiness: false)
    }

    private static func write(_ settings: HarborSettings, to url: URL, requireConnection: Bool, validateBusiness: Bool) throws {
        if validateBusiness { try SettingsValidator.validate(settings, requireConnection: requireConnection) }
        else { try SettingsValidator.validateExecutables(settings) }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let encoded: [String: Any]
        do {
            let value = try JSONSerialization.jsonObject(with: encoder.encode(settings))
            guard let object = value as? [String: Any] else { throw HarborSettingsError.io }
            try rejectSecretKeys(object)
            encoded = object
        } catch let error as HarborSettingsError {
            throw error
        } catch {
            throw HarborSettingsError.io
        }

        let target = url.standardizedFileURL
        let directory = target.deletingLastPathComponent()
        let fileManager = FileManager.default
        do { try fileManager.createDirectory(at: directory, withIntermediateDirectories: true) } catch { throw HarborSettingsError.io }
        var document = encoded
        if fileManager.fileExists(atPath: target.path) {
            let existingData: Data
            do {
                let size = try target.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                guard size <= 65536 else { throw HarborSettingsError.corrupt }
                existingData = try Data(contentsOf: target)
            } catch let error as HarborSettingsError {
                throw error
            } catch {
                throw HarborSettingsError.io
            }
            do {
                let value = try JSONSerialization.jsonObject(with: existingData)
                try rejectSecretKeys(value)
                guard let existing = value as? [String: Any] else { throw HarborSettingsError.corrupt }
                document = merge(existing, with: encoded)
            } catch let error as HarborSettingsError {
                throw error
            } catch {
                throw HarborSettingsError.corrupt
            }
        }
        let data: Data
        do { data = try JSONSerialization.data(withJSONObject: document, options: [.prettyPrinted, .sortedKeys]) } catch { throw HarborSettingsError.io }
        let temporary = directory.appendingPathComponent(".settings.\(UUID().uuidString).tmp")
        do {
            try data.write(to: temporary, options: .atomic)
            if fileManager.fileExists(atPath: target.path) {
                _ = try fileManager.replaceItemAt(target, withItemAt: temporary)
            } else {
                try fileManager.moveItem(at: temporary, to: target)
            }
        } catch {
            try? fileManager.removeItem(at: temporary)
            throw HarborSettingsError.io
        }
    }

    private static func merge(_ original: [String: Any], with known: [String: Any]) -> [String: Any] {
        var result = original
        for (key, value) in known {
            if let knownObject = value as? [String: Any], let originalObject = result[key] as? [String: Any] {
                result[key] = merge(originalObject, with: knownObject)
            } else {
                result[key] = value
            }
        }
        return result
    }

    private static func rejectSecretKeys(_ value: Any) throws {
        if let object = value as? [String: Any] {
            for (key, child) in object {
                let lower = key.lowercased()
                if ["api_key", "apikey", "secret", "token", "password", "passwd", "auth_token", "private_key"].contains(where: lower.contains) {
                    throw HarborSettingsError.secretKey
                }
                try rejectSecretKeys(child)
            }
        } else if let array = value as? [Any] {
            for child in array { try rejectSecretKeys(child) }
        }
    }
}

public struct HarborComponent: Codable, Equatable, Identifiable {
    public var name: String
    public var state: String
    public var message: String
    public var id: String { name }

    public init(name: String = "", state: String = "stopped", message: String = "") { self.name = name; self.state = state; self.message = message }
    enum CodingKeys: String, CodingKey { case name, state, message }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "stopped"
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
    }
}

public struct HarborHarness: Codable, Equatable, Identifiable {
    public var name: String
    public var available: Bool
    public var status: String
    public var summary: String
    public var runningJobIDs: [String]
    public var runningJobs: Int
    public var activityFresh: Bool
    public var id: String { name }

    public init(name: String = "", available: Bool = false, status: String = "Not detected", summary: String = "", runningJobIDs: [String] = [], runningJobs: Int = 0, activityFresh: Bool = false) {
        self.name = name; self.available = available; self.status = status; self.summary = summary
        self.runningJobIDs = runningJobIDs; self.runningJobs = max(0, runningJobs); self.activityFresh = activityFresh
    }
    enum CodingKeys: String, CodingKey {
        case name, available, status, summary
        case runningJobIDs = "running_job_ids", runningJobs = "running_jobs", activityFresh = "activity_fresh"
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        available = try c.decodeIfPresent(Bool.self, forKey: .available) ?? false
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "Not detected"
        summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? ""
        runningJobIDs = try c.decodeIfPresent([String].self, forKey: .runningJobIDs) ?? []
        runningJobs = max(0, try c.decodeIfPresent(Int.self, forKey: .runningJobs) ?? 0)
        activityFresh = try c.decodeIfPresent(Bool.self, forKey: .activityFresh) ?? false
    }

    public var runningJobsLabel: String? {
        guard activityFresh, runningJobs > 0 else { return nil }
        let suffixes = runningJobIDs.filter { !$0.isEmpty }.map { String($0.suffix(4)) }
        guard !suffixes.isEmpty else { return nil }
        return "\(runningJobs) running · \(suffixes.joined(separator: ", "))"
    }
}

public struct HarborRuntimePaths: Codable, Equatable {
    public var state: String
    public var jobs: String
    public var control: String
    public var logs: String
    public init(state: String = "", jobs: String = "", control: String = "", logs: String = "") { self.state = state; self.jobs = jobs; self.control = control; self.logs = logs }
}

public struct HarborStatusSnapshot: Codable, Equatable {
    public var state: String
    public var components: [String: HarborComponent]
    public var harnesses: [HarborHarness]
    public var runtimeVersion: String
    public var paths: HarborRuntimePaths
    public var setupRequired: Bool

    public init(state: String = "stopped", components: [String: HarborComponent] = ["mcp": .init(name: "mcp"), "daemon": .init(name: "daemon"), "tunnel": .init(name: "tunnel")], harnesses: [HarborHarness] = [], runtimeVersion: String = "", paths: HarborRuntimePaths = .init(), setupRequired: Bool = true) {
        self.state = state; self.components = components; self.harnesses = harnesses; self.runtimeVersion = runtimeVersion; self.paths = paths; self.setupRequired = setupRequired
    }

    enum CodingKeys: String, CodingKey { case state, components, harnesses, runtimeVersion = "runtime_version", paths, setupRequired = "setup_required" }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "stopped"
        components = try c.decodeIfPresent([String: HarborComponent].self, forKey: .components) ?? HarborStatusSnapshot().components
        harnesses = try c.decodeIfPresent([HarborHarness].self, forKey: .harnesses) ?? []
        runtimeVersion = try c.decodeIfPresent(String.self, forKey: .runtimeVersion) ?? ""
        paths = try c.decodeIfPresent(HarborRuntimePaths.self, forKey: .paths) ?? .init()
        setupRequired = try c.decodeIfPresent(Bool.self, forKey: .setupRequired) ?? true
    }
}

public struct HarborTelemetry: Codable, Equatable {
    public var harnesses: [HarborHarness]
    public var agyModels: [String]
    public init(harnesses: [HarborHarness] = [], agyModels: [String] = []) { self.harnesses = harnesses; self.agyModels = agyModels }
    enum CodingKeys: String, CodingKey { case harnesses, agyModels = "agy_models" }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        harnesses = try c.decodeIfPresent([HarborHarness].self, forKey: .harnesses) ?? []
        agyModels = try c.decodeIfPresent([String].self, forKey: .agyModels) ?? []
    }
}

public struct HarborTunnelTest: Codable, Equatable {
    public var ok: Bool
    public var checks: [String: Bool]
    public var message: String
    public init(ok: Bool = false, checks: [String: Bool] = [:], message: String = "") { self.ok = ok; self.checks = checks; self.message = message }
}

public struct HarborSettingsValidation: Codable, Equatable {
    public let ok: Bool
    public let settings: HarborSettings?
    public let errors: [String]

    public init(ok: Bool, settings: HarborSettings? = nil, errors: [String] = []) {
        self.ok = ok; self.settings = settings; self.errors = errors
    }
}

fileprivate enum SettingsMutation {
    static func commit(_ candidate: HarborSettings, old: HarborSettings, tunnelSecret: String?, customSecret: String?, deleteTunnel: Bool, deleteCustom: Bool, requireConnection: Bool, save: () throws -> Void) throws {
        guard tunnelSecret == nil || !deleteTunnel else { throw HarborSettingsError.invalid("Tunnel credential cannot be replaced and deleted in the same change.") }
        guard customSecret == nil || !deleteCustom else { throw HarborSettingsError.invalid("Custom credential cannot be replaced and deleted in the same change.") }
        let tunnelRequired = requireConnection || candidate.connection.requiresCredential
        let customRequired = candidate.codex.custom.enabled || candidate.codex.routingMode == HarborRoute.custom.rawValue || candidate.codex.routingMode == HarborRoute.officialThenCustom.rawValue
        let oldTunnel = try readIfNeeded(HarborCredentialTarget.tunnel, needed: tunnelSecret != nil || deleteTunnel)
        let oldCustom = try readIfNeeded(HarborCredentialTarget.codexCustom, needed: customSecret != nil || deleteCustom)
        if tunnelRequired, !(try candidatePresence(HarborCredentialTarget.tunnel, replacement: tunnelSecret, delete: deleteTunnel, required: true)) { throw HarborSettingsError.invalid("Tunnel Runtime Key is required.") }
        if customRequired, !(try candidatePresence(HarborCredentialTarget.codexCustom, replacement: customSecret, delete: deleteCustom, required: true)) { throw HarborSettingsError.invalid("Custom Codex API key is required for this route.") }

        var changed: [String] = []
        do {
            if let value = tunnelSecret { try HarborKeychain.store(HarborCredentialTarget.tunnel, secret: value); changed.append(HarborCredentialTarget.tunnel) }
            else if deleteTunnel { try HarborKeychain.delete(HarborCredentialTarget.tunnel); changed.append(HarborCredentialTarget.tunnel) }
            if let value = customSecret { try HarborKeychain.store(HarborCredentialTarget.codexCustom, secret: value); changed.append(HarborCredentialTarget.codexCustom) }
            else if deleteCustom { try HarborKeychain.delete(HarborCredentialTarget.codexCustom); changed.append(HarborCredentialTarget.codexCustom) }
            try save()
        } catch {
            var rollbackFailed = false
            for ref in changed {
                do {
                    let previous = ref == HarborCredentialTarget.tunnel ? oldTunnel : oldCustom
                    if let previous { try HarborKeychain.store(ref, secret: previous) } else { try HarborKeychain.delete(ref) }
                } catch { rollbackFailed = true }
            }
            if rollbackFailed { throw HarborSettingsError.invalid("Settings were not saved and Keychain rollback failed. Re-enter the affected credentials before starting Harbor.") }
            throw error is HarborSettingsError ? error : HarborSettingsError.io
        }
    }

    static func candidatePresence(_ target: String, replacement: String?, delete: Bool, required: Bool) throws -> Bool {
        if let replacement { return !replacement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        if delete { return false }
        guard required else { return false }
        return try HarborKeychain.contains(target)
    }

    private static func readIfNeeded(_ ref: String, needed: Bool) throws -> String? {
        guard needed else { return nil }
        do { return try HarborKeychain.read(ref) } catch { throw HarborSettingsError.invalid("Secure credential store is unavailable; changes were not applied.") }
    }
}

@MainActor
public final class HarborModel: ObservableObject {
    public let bridge: HarborBridge
    @Published public private(set) var settings: HarborSettings
    @Published public private(set) var status = HarborStatusSnapshot()
    @Published public private(set) var harnesses: [HarborHarness] = []
    @Published public private(set) var telemetry = HarborTelemetry()
    @Published public private(set) var logText = ""
    @Published public private(set) var diagnosticsText = ""
    @Published public private(set) var tunnelCredentialConfigured = false
    @Published public private(set) var customCredentialConfigured = false
    @Published public private(set) var launchAtLogin = false
    @Published public private(set) var connected = false
    @Published public private(set) var busy = false
    @Published public var lastError: String?

    private var pollTimer: Timer?
    private var launched = false
    private var refreshGeneration = 0
    private var hasStatusHarnessSnapshot = false
    public private(set) var readyToQuit = false

    public var setupRequired: Bool { !settings.macos.setupComplete }
    public var runtimePath: String { bridge.runtimeURL?.path ?? "Bundled HarborRuntime/harbor-runtime" }

    public init() {
        do { settings = try SettingsStore.load() }
        catch { settings = HarborSettings(); lastError = "Existing settings could not be loaded; the file was left unchanged." }
        if #available(macOS 13.0, *) { launchAtLogin = SMAppService.mainApp.status == .enabled }
        bridge = HarborBridge()
        bridge.onSnapshot = { [weak self] snapshot in
            self?.status = snapshot
        }
        bridge.onConnectionChanged = { [weak self] value in
            self?.connected = value
            if !value {
                self?.refreshGeneration += 1
                self?.invalidateRuntimeActivity()
            }
        }
        bridge.onError = { [weak self] message in self?.lastError = message }
    }

    public func launch(openDashboard: @escaping () -> Void) {
        guard !launched else { return }
        launched = true
        refreshCredentialState()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.refresh() }
        }
        if settings.macos.openDashboard || !settings.macos.setupComplete { openDashboard() }
        bridge.start { [weak self] result in
            guard let self else { return }
            if case .success = result {
                self.refresh()
                if self.settings.macos.startAtLaunch && self.settings.macos.setupComplete { self.startHarbor() }
            }
        }
    }

    public func refresh() {
        guard connected && !busy else { return }
        refreshGeneration += 1
        let generation = refreshGeneration
        invalidateRuntimeActivity()
        bridge.statusSnapshot { [weak self] result in
            guard let self, generation == self.refreshGeneration else { return }
            switch result {
            case .success(let snapshot): self.applyStatusSnapshot(snapshot)
            case .failure(let error): self.lastError = error.message; self.hasStatusHarnessSnapshot = true; self.invalidateRuntimeActivity()
            }
        }
        bridge.telemetry { [weak self] result in
            guard let self, generation == self.refreshGeneration else { return }
            if case .success(let value) = result {
                self.applyTelemetry(value)
            }
        }
    }

    public func startHarbor() { status.state = "starting"; runRuntimeOperation(bridge.startRuntime) }
    public func stopHarbor() { status.state = "stopping"; runRuntimeOperation(bridge.stopRuntime) }
    public func restartHarbor() { status.state = "restarting"; runRuntimeOperation(bridge.restartRuntime) }

    public func testDraft(_ draft: HarborSettings, tunnelSecret: String, customSecret: String,
                          completion: @escaping (Result<HarborTunnelTest, Error>) -> Void) {
        busy = true
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let customRequired = draft.codex.custom.enabled || draft.codex.routingMode == HarborRoute.custom.rawValue || draft.codex.routingMode == HarborRoute.officialThenCustom.rawValue
                let tunnelPresent = try SettingsMutation.candidatePresence(HarborCredentialTarget.tunnel, replacement: tunnelSecret, delete: false, required: true)
                let customPresent = try SettingsMutation.candidatePresence(HarborCredentialTarget.codexCustom, replacement: customSecret, delete: false, required: customRequired)
                DispatchQueue.main.async {
                    self.bridge.testConnection(settings: draft, credentials: ["tunnel": tunnelPresent, "custom": customPresent]) { result in
                        self.busy = false
                        completion(result.mapError { $0 as Error })
                    }
                }
            } catch {
                DispatchQueue.main.async { self.busy = false; completion(.failure(HarborSettingsError.invalid("Draft validation failed. Check paths, connection and Keychain availability."))) }
            }
        }
    }

    public func testConnection(completion: ((Result<HarborTunnelTest, BridgeError>) -> Void)? = nil) {
        bridge.testConnection { [weak self] result in
            if case .failure(let error) = result { self?.lastError = error.message }
            completion?(result)
        }
    }

    public func tailLogs(component: String, lines: Int = 80) {
        bridge.tailLogs(component: component, lines: lines) { [weak self] result in
            switch result {
            case .success(let value): self?.logText = value
            case .failure(let error): self?.lastError = error.message
            }
        }
    }

    public func runDiagnostics() {
        bridge.diagnostics { [weak self] result in
            switch result {
            case .success(let object): self?.diagnosticsText = JSONValue.object(object).prettyString
            case .failure(let error): self?.lastError = error.message
            }
        }
    }

    public func detectExecutables(completion: @escaping ([String: String]) -> Void) {
        bridge.diagnostics { result in
            let found: [String: String]
            switch result {
            case .success(let object):
                found = object["detected_executables"]?.objectValue?.compactMapValues(\.stringValue) ?? [:]
            case .failure: found = [:]
            }
            completion(found)
        }
    }

    public func applySettings(_ draft: HarborSettings, tunnelSecret: String? = nil, customSecret: String? = nil, deleteTunnel: Bool = false, deleteCustom: Bool = false, setupComplete: Bool? = nil, requireConnection: Bool = false, completion: @escaping (Result<Void, Error>) -> Void) {
        var candidate = draft
        if let setupComplete { candidate.macos.setupComplete = setupComplete }
        busy = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let old = try SettingsStore.load()
                let tunnelRequired = requireConnection || candidate.connection.requiresCredential
                let customRequired = candidate.codex.custom.enabled || candidate.codex.routingMode == HarborRoute.custom.rawValue || candidate.codex.routingMode == HarborRoute.officialThenCustom.rawValue
                let credentials = [
                    "tunnel": try SettingsMutation.candidatePresence(HarborCredentialTarget.tunnel, replacement: tunnelSecret, delete: deleteTunnel, required: tunnelRequired),
                    "custom": try SettingsMutation.candidatePresence(HarborCredentialTarget.codexCustom, replacement: customSecret, delete: deleteCustom, required: customRequired)
                ]
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.bridge.validateSettings(settings: candidate, credentials: credentials, requireConnection: requireConnection) { [weak self] result in
                        DispatchQueue.main.async {
                            guard let self else { return }
                            switch result {
                            case .failure(let error):
                                self.busy = false
                                completion(.failure(error))
                            case .success(let validation):
                                guard validation.ok, let normalized = validation.settings else {
                                    let message = validation.errors.isEmpty ? "Runtime rejected the settings." : validation.errors.joined(separator: " ")
                                    self.busy = false
                                    completion(.failure(HarborSettingsError.invalid(message)))
                                    return
                                }
                                DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                                    do {
                                        try SettingsValidator.validateExecutables(normalized)
                                        try SettingsMutation.commit(normalized, old: old, tunnelSecret: tunnelSecret, customSecret: customSecret, deleteTunnel: deleteTunnel, deleteCustom: deleteCustom, requireConnection: requireConnection) {
                                            try SettingsStore.saveAfterRuntimeValidation(normalized, requireConnection: requireConnection)
                                        }
                                        DispatchQueue.main.async {
                                            guard let self else { return }
                                            self.settings = normalized
                                            self.refreshCredentialState()
                                            self.bridge.restartBridge { [weak self] result in
                                                guard let self else { return }
                                                self.busy = false
                                                switch result {
                                                case .success: completion(.success(()))
                                                case .failure(let error): self.lastError = error.message; completion(.failure(BridgeError(code: "restart_failed", message: "Settings were saved, but the Harbor runtime could not restart.")))
                                                }
                                            }
                                        }
                                    } catch let error as HarborSettingsError {
                                        DispatchQueue.main.async { [weak self] in self?.busy = false; completion(.failure(error)) }
                                    } catch {
                                        DispatchQueue.main.async { [weak self] in self?.busy = false; completion(.failure(HarborSettingsError.io)) }
                                    }
                                }
                            }
                        }
                    }
                }
            } catch let error as HarborSettingsError {
                DispatchQueue.main.async { [weak self] in self?.busy = false; completion(.failure(error)) }
            } catch {
                DispatchQueue.main.async { [weak self] in self?.busy = false; completion(.failure(HarborSettingsError.io)) }
            }
        }
    }

    public func finishSetup(_ draft: HarborSettings, completion: @escaping (Result<Void, Error>) -> Void) {
        applySettings(draft, setupComplete: true, requireConnection: true, completion: completion)
    }

    public func setLaunchAtLogin(_ enabled: Bool) {
        guard #available(macOS 13.0, *) else { return }
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            launchAtLogin = enabled
        } catch {
            launchAtLogin = SMAppService.mainApp.status == .enabled
            lastError = "Launch at login could not be updated."
        }
    }

    public func refreshCredentialState() {
        DispatchQueue.global(qos: .utility).async {
            let tunnel = Self.hasCredential(HarborCredentialTarget.tunnel)
            let custom = Self.hasCredential(HarborCredentialTarget.codexCustom)
            DispatchQueue.main.async { [weak self] in
                self?.tunnelCredentialConfigured = tunnel
                self?.customCredentialConfigured = custom
            }
        }
    }

    public func stopThenQuit() {
        busy = true
        bridge.shutdown { [weak self] result in
            guard let self else { return }
            self.busy = false
            switch result {
            case .failure(let error): self.lastError = error.message
            case .success:
                self.readyToQuit = true
                NSApplication.shared.terminate(nil)
            }
        }
    }

    private func runRuntimeOperation(_ operation: @escaping (@escaping (Result<HarborStatusSnapshot, BridgeError>) -> Void) -> Void) {
        refreshGeneration += 1
        invalidateRuntimeActivity()
        busy = true
        bridge.start { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error): self.busy = false; self.lastError = error.message
            case .success: operation { [weak self] result in
                guard let self else { return }
                self.busy = false
                switch result {
                case .success(let snapshot): self.applyStatusSnapshot(snapshot)
                case .failure(let error): self.lastError = error.message
                }
            }
            }
        }
    }

    private func applyStatusSnapshot(_ snapshot: HarborStatusSnapshot) {
        status = snapshot
        hasStatusHarnessSnapshot = true
        harnesses = snapshot.harnesses
    }

    private func applyTelemetry(_ value: HarborTelemetry) {
        telemetry = value
        guard hasStatusHarnessSnapshot else { harnesses = value.harnesses; return }
        let capabilityByName = Dictionary(uniqueKeysWithValues: value.harnesses.map { ($0.name, $0) })
        harnesses = harnesses.map { current in
            guard var merged = capabilityByName[current.name] else { return current }
            merged.runningJobIDs = current.runningJobIDs
            merged.runningJobs = current.runningJobs
            merged.activityFresh = current.activityFresh
            return merged
        }
    }

    private func invalidateRuntimeActivity() {
        harnesses = harnesses.map {
            var row = $0
            row.runningJobIDs = []
            row.runningJobs = 0
            row.activityFresh = false
            return row
        }
    }

    nonisolated private static func hasCredential(_ target: String) -> Bool {
        do { return try HarborKeychain.contains(target) } catch { return false }
    }
}
