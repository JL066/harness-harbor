import Foundation
import Darwin

// Local plaintext credentials are an explicit macOS storage policy, not a
// fallback. Never consult the old Keychain or import its values automatically.
enum HarborCredentials {
    private static let names = [
        "Harness-Harbor:tunnel:runtime_key": "tunnel-runtime-key.txt",
        "Harness-Harbor:codex:custom_api_key": "codex-custom-api-key.txt"
    ]
    static var directory: URL { SettingsStore.settingsDirectory().appendingPathComponent("credentials", isDirectory: true) }

    static func runtimeSecrets(for settings: HarborSettings, readSecret: (String) throws -> String? = { try read($0) }) throws -> [String: String] {
        var result: [String: String] = [:]
        if settings.connection.requiresCredential, let value = try readSecret(HarborCredentialTarget.tunnel), !value.isEmpty {
            result["TUNNEL_RUNTIME_KEY"] = value
        }
        if settings.codex.custom.enabled || settings.codex.routingMode == HarborRoute.custom.rawValue || settings.codex.routingMode == HarborRoute.officialThenCustom.rawValue,
           let value = try readSecret(HarborCredentialTarget.codexCustom), !value.isEmpty {
            result["HARBOR_CODEX_CUSTOM_API_KEY"] = value
        }
        return result
    }

    struct Failure: LocalizedError {
        var errorDescription: String? { "Local credential file is unavailable or has unsafe permissions. Check the credentials directory and save the key again." }
    }

    private static func name(_ target: String) throws -> String {
        guard let name = names[target] else { throw Failure() }
        return name
    }

    private static func openDirectory(_ root: URL, create: Bool) throws -> Int32 {
        if create {
            try FileManager.default.createDirectory(at: root.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            if mkdir(root.path, 0o700) != 0 && errno != EEXIST { throw Failure() }
        }
        let fd = open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        if fd < 0 {
            if !create && errno == ENOENT { return -1 }
            throw Failure()
        }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
            close(fd); throw Failure()
        }
        return fd
    }

    private static func openFile(_ name: String, in directory: Int32) throws -> Int32 {
        let fd = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        if fd < 0 {
            if errno == ENOENT { return -1 }
            throw Failure()
        }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              info.st_uid == getuid(), info.st_mode & 0o077 == 0,
              info.st_nlink == 1, info.st_size <= 65536 else {
            close(fd); throw Failure()
        }
        return fd
    }

    static func contains(_ target: String, at root: URL = directory) throws -> Bool {
        let filename = try name(target)
        let dir = try openDirectory(root, create: false)
        guard dir >= 0 else { return false }
        defer { close(dir) }
        let fd = try openFile(filename, in: dir)
        guard fd >= 0 else { return false }
        close(fd)
        return true
    }

    static func read(_ target: String, at root: URL = directory) throws -> String? {
        let filename = try name(target)
        let dir = try openDirectory(root, create: false)
        guard dir >= 0 else { return nil }
        defer { close(dir) }
        let fd = try openFile(filename, in: dir)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
        guard let data = try file.read(upToCount: 65537), data.count <= 65536,
              let value = String(data: data, encoding: .utf8), !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw Failure() }
        return value
    }

    static func store(_ target: String, secret: String, at root: URL = directory) throws {
        let filename = try name(target)
        let data = Data(secret.utf8)
        guard !secret.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, data.count <= 65536 else { throw Failure() }
        let dir = try openDirectory(root, create: true)
        defer { close(dir) }
        let existing = try openFile(filename, in: dir)
        if existing >= 0 { close(existing) }
        let temp = ".credential-\(UUID().uuidString).tmp"
        let fd = openat(dir, temp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw Failure() }
        defer { close(fd); unlinkat(dir, temp, 0) }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
        try file.write(contentsOf: data)
        guard fsync(fd) == 0, renameat(dir, temp, dir, filename) == 0 else { throw Failure() }
    }

    static func delete(_ target: String, at root: URL = directory) throws {
        let filename = try name(target)
        let dir = try openDirectory(root, create: false)
        guard dir >= 0 else { return }
        defer { close(dir) }
        let existing = try openFile(filename, in: dir)
        guard existing >= 0 else { return }
        close(existing)
        guard unlinkat(dir, filename, 0) == 0 || errno == ENOENT else { throw Failure() }
    }
}
