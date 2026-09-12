import Foundation
import Security
import LocalAuthentication

enum HarborKeychain {
    static let service = "com.jl066.harness-harbor"
    static let targets: Set<String> = ["Harness-Harbor:tunnel:runtime_key", "Harness-Harbor:codex:custom_api_key"]

    struct Failure: LocalizedError {
        let status: OSStatus
        var errorDescription: String? { "Keychain operation failed (\(status)). Unlock your login keychain and retry." }
    }

    private static func query(_ target: String) throws -> [String: Any] {
        guard targets.contains(target) else { throw Failure(status: errSecParam) }
        return [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service, kSecAttrAccount as String: target]
    }

    // Presence is not permission to read the secret. Never show auth UI for badges
    // or draft validation; actual credential use still goes through read().
    static func contains(_ target: String, copyMatching: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus = SecItemCopyMatching) throws -> Bool {
        var request = try query(target)
        request[kSecReturnAttributes as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        let context = LAContext()
        context.interactionNotAllowed = true
        request[kSecUseAuthenticationContext as String] = context
        var result: CFTypeRef?
        let status = copyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return false }
        guard status == errSecSuccess else { throw Failure(status: status) }
        return true
    }

    static func read(_ target: String) throws -> String? {
        var request = try query(target)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data,
              let secret = String(data: data, encoding: .utf8) else { throw Failure(status: status) }
        return secret
    }

    static func store(_ target: String, secret: String) throws {
        guard !secret.isEmpty else { throw Failure(status: errSecParam) }
        let request = try query(target)
        let changes = [kSecValueData as String: Data(secret.utf8)]
        var status = SecItemUpdate(request as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(request.merging(changes) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Failure(status: status) }
    }

    static func delete(_ target: String) throws {
        let status = SecItemDelete(try query(target) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw Failure(status: status) }
    }
}
