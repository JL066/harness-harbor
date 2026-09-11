import Combine
import Darwin
import Foundation

public enum JSONValue: Codable, Equatable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null; return }
        if let bool = try? value.decode(Bool.self) { self = .bool(bool); return }
        if let int = try? value.decode(Int.self) { self = .number(Double(int)); return }
        if let double = try? value.decode(Double.self) { self = .number(double); return }
        if let string = try? value.decode(String.self) { self = .string(string); return }
        if let array = try? value.decode([JSONValue].self) { self = .array(array); return }
        if let object = try? value.decode([String: JSONValue].self) { self = .object(object); return }
        throw DecodingError.dataCorruptedError(in: value, debugDescription: "Unsupported JSON value")
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .object(let object): try value.encode(object)
        case .array(let array): try value.encode(array)
        case .string(let string): try value.encode(string)
        case .number(let number): try value.encode(number)
        case .bool(let bool): try value.encode(bool)
        case .null: try value.encodeNil()
        }
    }

    public var objectValue: [String: JSONValue]? { if case .object(let value) = self { return value }; return nil }
    public var arrayValue: [JSONValue]? { if case .array(let value) = self { return value }; return nil }
    public var stringValue: String? { if case .string(let value) = self { return value }; return nil }
    public var boolValue: Bool? { if case .bool(let value) = self { return value }; return nil }
    public var intValue: Int? {
        guard case .number(let value) = self else { return nil }
        return Int(exactly: value)
    }

    public func decoded<T: Decodable>(_ type: T.Type) throws -> T {
        try JSONDecoder().decode(T.self, from: JSONEncoder().encode(self))
    }

    public var prettyString: String {
        guard let data = try? JSONEncoder.pretty.encode(self), let string = String(data: data, encoding: .utf8) else { return "{}" }
        return string
    }
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

public struct BridgeRequest: Codable, Equatable {
    public let v: Int
    public let id: String
    public let method: String
    public let params: [String: JSONValue]

    public init(v: Int = BridgeProtocol.version, id: String, method: String, params: [String: JSONValue]) {
        self.v = v; self.id = id; self.method = method; self.params = params
    }

    enum CodingKeys: String, CodingKey { case v, id, method, params }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard Set(c.allKeys.map(\.stringValue)) == Set(["v", "id", "method", "params"]) else { throw BridgeProtocolError.invalidRequest }
        v = try c.decode(Int.self, forKey: .v)
        id = try c.decode(String.self, forKey: .id)
        method = try c.decode(String.self, forKey: .method)
        params = try c.decode([String: JSONValue].self, forKey: .params)
        try BridgeProtocol.validate(v: v, id: id, method: method, params: params)
    }
}

public struct BridgeErrorPayload: Codable, Equatable {
    public let code: String
    public let message: String

    public init(code: String, message: String) { self.code = code; self.message = message }
    enum CodingKeys: String, CodingKey { case code, message }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard Set(c.allKeys.map(\.stringValue)) == Set(["code", "message"]) else { throw BridgeProtocolError.invalidResponse }
        code = try c.decode(String.self, forKey: .code)
        message = try c.decode(String.self, forKey: .message)
    }
}

public struct BridgeResponse: Codable, Equatable {
    public let v: Int
    public let id: String
    public let ok: Bool
    public let result: JSONValue?
    public let error: BridgeErrorPayload?

    public init(v: Int = BridgeProtocol.version, id: String, ok: Bool, result: JSONValue? = nil, error: BridgeErrorPayload? = nil) {
        self.v = v; self.id = id; self.ok = ok; self.result = result; self.error = error
    }

    enum CodingKeys: String, CodingKey { case v, id, ok, result, error }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let keys = Set(c.allKeys.map(\.stringValue))
        guard keys.contains("v"), keys.contains("id"), keys.contains("ok"), keys.count == 5 || keys.count == 4 else { throw BridgeProtocolError.invalidResponse }
        v = try c.decode(Int.self, forKey: .v)
        id = try c.decode(String.self, forKey: .id)
        ok = try c.decode(Bool.self, forKey: .ok)
        if ok {
            guard keys == Set(["v", "id", "ok", "result"]) else { throw BridgeProtocolError.invalidResponse }
            result = try c.decodeIfPresent(JSONValue.self, forKey: .result) ?? .null
            error = nil
        } else {
            guard keys == Set(["v", "id", "ok", "error"]) else { throw BridgeProtocolError.invalidResponse }
            result = nil
            error = try c.decode(BridgeErrorPayload.self, forKey: .error)
        }
        guard v == BridgeProtocol.version, UUID(uuidString: id) != nil else { throw BridgeProtocolError.incompatibleProtocol }
    }
}

public enum BridgeProtocolError: Error, Equatable {
    case invalidRequest
    case invalidResponse
    case incompatibleProtocol
    case unknownMethod
    case invalidParams
    case inputLimit
}

public enum BridgeProtocol {
    public static let version = 1
    public static let maxLineBytes = 262_144
    public static let requestTimeout: TimeInterval = 10
    public static let lifecycleTimeout: TimeInterval = 30
    public static let methods = ["hello", "status.snapshot", "runtime.start", "runtime.stop", "runtime.restart", "harness.telemetry", "tunnel.test", "settings.validate", "logs.tail", "diagnostics.run", "shutdown"]

    public static func makeRequest(method: String, params: [String: JSONValue] = [:]) throws -> BridgeRequest {
        let request = BridgeRequest(id: UUID().uuidString, method: method, params: params)
        try validate(v: request.v, id: request.id, method: method, params: params)
        return request
    }

    public static func encodeRequest(_ request: BridgeRequest) throws -> Data {
        try validate(v: request.v, id: request.id, method: request.method, params: request.params)
        let data = try JSONEncoder().encode(request)
        guard data.count <= maxLineBytes else { throw BridgeProtocolError.inputLimit }
        return data + Data([10])
    }

    public static func decodeResponse(_ data: Data) throws -> BridgeResponse {
        guard data.count <= maxLineBytes else { throw BridgeProtocolError.inputLimit }
        return try JSONDecoder().decode(BridgeResponse.self, from: data)
    }

    public static func validate(v: Int, id: String, method: String, params: [String: JSONValue]) throws {
        guard v == version, UUID(uuidString: id) != nil else { throw BridgeProtocolError.incompatibleProtocol }
        guard methods.contains(method) else { throw BridgeProtocolError.unknownMethod }
        if method == "logs.tail" {
            guard Set(params.keys) == Set(["component", "lines"]), let component = params["component"]?.stringValue, ["runtime", "daemon", "tunnel"].contains(component), let lines = params["lines"]?.intValue, (1...200).contains(lines) else { throw BridgeProtocolError.invalidParams }
        } else if method == "settings.validate" {
            guard Set(params.keys) == Set(["settings", "credentials", "require_connection"]), params["settings"]?.objectValue != nil,
                  let credentials = params["credentials"]?.objectValue, Set(credentials.keys) == Set(["tunnel", "custom"]),
                  credentials.values.allSatisfy({ if case .bool = $0 { return true }; return false }),
                  params["require_connection"]?.boolValue != nil else { throw BridgeProtocolError.invalidParams }
        } else if method == "tunnel.test" && !params.isEmpty {
            guard Set(params.keys) == Set(["settings", "credentials"]), params["settings"]?.objectValue != nil,
                  let credentials = params["credentials"]?.objectValue, Set(credentials.keys) == Set(["tunnel", "custom"]),
                  credentials.values.allSatisfy({ if case .bool = $0 { return true }; return false }) else { throw BridgeProtocolError.invalidParams }
        } else if !params.isEmpty {
            throw BridgeProtocolError.invalidParams
        }
    }

    public static func validateHello(_ result: JSONValue) throws -> (runtimeVersion: String, buildVersion: String) {
        guard let object = result.objectValue,
              object["protocol_version"]?.intValue == version,
              let runtimeVersion = object["runtime_version"]?.stringValue, runtimeVersion == "1.1.0",
              let buildVersion = object["build_version"]?.stringValue,
              object["platform"]?.stringValue == "darwin",
              let capabilities = object["capabilities"]?.arrayValue?.compactMap(\.stringValue),
              Set(methods).isSubset(of: Set(capabilities)) else { throw BridgeProtocolError.incompatibleProtocol }
        return (runtimeVersion, buildVersion)
    }
}

public struct BridgeError: Error, LocalizedError {
    public let code: String
    public let message: String
    public var errorDescription: String? { message }
    public init(code: String, message: String) { self.code = code; self.message = message }
}

public final class HarborBridge: ObservableObject {
    public let runtimeURL: URL?
    @Published public private(set) var connected = false
    @Published public private(set) var runtimeVersion = ""

    public var onSnapshot: ((HarborStatusSnapshot) -> Void)?
    public var onConnectionChanged: ((Bool) -> Void)?
    public var onError: ((String) -> Void)?

    private let ioQueue = DispatchQueue(label: "com.jl066.harness-harbor.bridge", qos: .userInitiated)
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var errorOutput: FileHandle?
    private var inputBuffer: [UInt8] = []
    private var pending: [String: PendingRequest] = [:]
    private var readyWaiters: [((Result<Void, BridgeError>) -> Void)] = []
    private var ready = false
    private var handshaking = false
    private var generation = 0

    private struct PendingRequest {
        let method: String
        let timeout: DispatchWorkItem
        let completion: (Result<JSONValue, BridgeError>) -> Void
    }

    public init() {
        if let override = ProcessInfo.processInfo.environment["HARBOR_RUNTIME_EXE"], !override.isEmpty {
            runtimeURL = URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        } else {
            runtimeURL = Bundle.main.resourceURL?.appendingPathComponent("HarborRuntime/harbor-runtime")
        }
    }

    public func start(completion: ((Result<Void, BridgeError>) -> Void)? = nil) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            if let completion { self.readyWaiters.append(completion) }
            self.startOnQueue()
        }
    }

    public func restartBridge(completion: @escaping (Result<Void, BridgeError>) -> Void) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            let continueWithStart = {
                self.stopProcessOnQueue()
                self.readyWaiters.append(completion)
                self.startOnQueue()
            }
            guard self.process != nil else { self.readyWaiters.append(completion); self.startOnQueue(); return }
            if self.ready {
                self.callOnQueue(method: "shutdown", params: [:], timeout: BridgeProtocol.lifecycleTimeout) { [weak self] result in
                    guard let self else { return }
                    if case .failure(let error) = result { self.report(error.message) }
                    continueWithStart()
                }
            } else {
                continueWithStart()
            }
        }
    }

    public func shutdown(completion: @escaping (Result<Void, BridgeError>) -> Void) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            guard self.ready else {
                self.stopProcessOnQueue()
                self.deliver(completion, .success(()))
                return
            }
            self.callOnQueue(method: "shutdown", params: [:], timeout: BridgeProtocol.lifecycleTimeout) { [weak self] result in
                guard let self else { return }
                self.stopProcessOnQueue()
                switch result {
                case .success: self.deliver(completion, .success(()))
                case .failure(let error): self.deliver(completion, .failure(error))
                }
            }
        }
    }

    public func statusSnapshot(completion: @escaping (Result<HarborStatusSnapshot, BridgeError>) -> Void) {
        callDecoded("status.snapshot", type: HarborStatusSnapshot.self, completion: completion) { [weak self] value in self?.publish(value) }
    }

    public func telemetry(completion: @escaping (Result<HarborTelemetry, BridgeError>) -> Void) {
        callDecoded("harness.telemetry", type: HarborTelemetry.self, completion: completion)
    }

    public func startRuntime(completion: @escaping (Result<HarborStatusSnapshot, BridgeError>) -> Void) { runtimeOperation("runtime.start", completion: completion) }
    public func stopRuntime(completion: @escaping (Result<HarborStatusSnapshot, BridgeError>) -> Void) { runtimeOperation("runtime.stop", completion: completion) }
    public func restartRuntime(completion: @escaping (Result<HarborStatusSnapshot, BridgeError>) -> Void) { runtimeOperation("runtime.restart", completion: completion) }

    public func tailLogs(component: String, lines: Int = 80, completion: @escaping (Result<String, BridgeError>) -> Void) {
        let params: [String: JSONValue] = ["component": .string(component), "lines": .number(Double(lines))]
        callDecoded("logs.tail", params: params, type: LogTail.self, completion: { result in
            switch result {
            case .success(let value): completion(.success(value.text))
            case .failure(let error): completion(.failure(error))
            }
        })
    }

    public func diagnostics(completion: @escaping (Result<[String: JSONValue], BridgeError>) -> Void) {
        callDecoded("diagnostics.run", type: [String: JSONValue].self, completion: completion)
    }

    public func testConnection(settings: HarborSettings? = nil, credentials: [String: Bool] = [:], completion: @escaping (Result<HarborTunnelTest, BridgeError>) -> Void) {
        var params: [String: JSONValue] = [:]
        if let settings {
            guard let data = try? JSONEncoder().encode(settings), let value = try? JSONDecoder().decode(JSONValue.self, from: data) else {
                completion(.failure(BridgeError(code: "invalid_settings", message: "Draft settings are invalid."))); return
            }
            params = ["settings": value, "credentials": .object(credentials.mapValues { .bool($0) })]
        }
        callDecoded("tunnel.test", params: params, type: HarborTunnelTest.self, completion: completion)
    }

    public func validateSettings(settings: HarborSettings, credentials: [String: Bool], requireConnection: Bool,
                                 completion: @escaping (Result<HarborSettingsValidation, BridgeError>) -> Void) {
        guard Set(credentials.keys) == Set(["tunnel", "custom"]) else {
            completion(.failure(BridgeError(code: "invalid_params", message: "Settings validation requires tunnel and custom credential presence flags.")))
            return
        }
        guard let data = try? JSONEncoder().encode(settings), let value = try? JSONDecoder().decode(JSONValue.self, from: data) else {
            completion(.failure(BridgeError(code: "invalid_settings", message: "Draft settings are invalid.")))
            return
        }
        let params: [String: JSONValue] = [
            "settings": value,
            "credentials": .object(credentials.mapValues { .bool($0) }),
            "require_connection": .bool(requireConnection)
        ]
        callDecoded("settings.validate", params: params, type: HarborSettingsValidation.self, completion: completion)
    }

    private struct LogTail: Codable { let text: String }

    private func runtimeOperation(_ method: String, completion: @escaping (Result<HarborStatusSnapshot, BridgeError>) -> Void) {
        start { [weak self] result in
            switch result {
            case .failure(let error): completion(.failure(error))
            case .success: self?.callDecoded(method, type: HarborStatusSnapshot.self, timeout: BridgeProtocol.lifecycleTimeout, completion: completion) { [weak self] value in self?.publish(value) }
            }
        }
    }

    private func callDecoded<T: Decodable>(_ method: String, params: [String: JSONValue] = [:], type: T.Type, timeout: TimeInterval = BridgeProtocol.requestTimeout, completion: @escaping (Result<T, BridgeError>) -> Void, onValue: ((T) -> Void)? = nil) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            self.callOnQueue(method: method, params: params, timeout: timeout) { [weak self] result in
                guard let self else { return }
                switch result {
                case .failure(let error): self.deliver(completion, .failure(error))
                case .success(let value):
                    do {
                        let decoded = try value.decoded(T.self)
                        onValue?(decoded)
                        self.deliver(completion, .success(decoded))
                    } catch {
                        self.deliver(completion, .failure(BridgeError(code: "invalid_result", message: "Runtime returned an invalid \(method) response.")))
                    }
                }
            }
        }
    }

    private func startOnQueue() {
        if ready { finishReady(.success(())); return }
        if process != nil { return }
        guard let runtimeURL, FileManager.default.isExecutableFile(atPath: runtimeURL.path) else {
            let error = BridgeError(code: "runtime_unavailable", message: "Harbor runtime is unavailable.")
            report(error.message); finishReady(.failure(error)); return
        }
        let childInput = Pipe()
        let childOutput = Pipe()
        let childError = Pipe()
        let child = Process()
        child.executableURL = runtimeURL
        child.arguments = ["bridge"]
        child.standardInput = childInput
        child.standardOutput = childOutput
        child.standardError = childError
        do {
            child.environment = try runtimeEnvironment()
            generation += 1
            let token = generation
            child.terminationHandler = { [weak self] process in
                self?.ioQueue.async { self?.processTerminated(process, generation: token) }
            }
            try child.run()
        } catch let error as BridgeError {
            report(error.message); finishReady(.failure(error)); return
        } catch {
            let failure = BridgeError(code: "runtime_start_failed", message: "Harbor runtime could not start.")
            report(failure.message); finishReady(.failure(failure)); return
        }
        process = child
        input = childInput.fileHandleForWriting
        output = childOutput.fileHandleForReading
        errorOutput = childError.fileHandleForReading
        inputBuffer.removeAll(keepingCapacity: true)
        let token = generation
        output?.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { self?.ioQueue.async { self?.processEOF(generation: token) } }
            else { self?.ioQueue.async { self?.consume(data, generation: token) } }
        }
        errorOutput?.readabilityHandler = { handle in _ = handle.availableData }
        handshaking = true
        callOnQueue(method: "hello", params: [:]) { [weak self] result in self?.finishHandshake(result) }
    }

    private func runtimeEnvironment() throws -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "TUNNEL_RUNTIME_KEY")
        environment.removeValue(forKey: "HARBOR_CODEX_CUSTOM_API_KEY")
        do {
            if let tunnel = try HarborKeychain.read(HarborCredentialTarget.tunnel), !tunnel.isEmpty { environment["TUNNEL_RUNTIME_KEY"] = tunnel }
            if let custom = try HarborKeychain.read(HarborCredentialTarget.codexCustom), !custom.isEmpty { environment["HARBOR_CODEX_CUSTOM_API_KEY"] = custom }
        } catch {
            throw BridgeError(code: "keychain_unavailable", message: "Secure credential store is unavailable.")
        }
        environment["HARBOR_USER_SETTINGS_DIR"] = SettingsStore.settingsDirectory().path
        return environment
    }

    private func finishHandshake(_ result: Result<JSONValue, BridgeError>) {
        switch result {
        case .failure:
            let error = BridgeError(code: "handshake_failed", message: "Harbor runtime handshake failed.")
            report(error.message); stopProcessOnQueue(); finishReady(.failure(error))
        case .success(let value):
            do {
                let hello = try BridgeProtocol.validateHello(value)
                ready = true; handshaking = false
                publishConnection(true); publishRuntimeVersion(hello.runtimeVersion)
                finishReady(.success(()))
                statusSnapshot { _ in }
            } catch {
                let error = BridgeError(code: "incompatible_protocol", message: "Harbor runtime protocol is incompatible.")
                report(error.message); stopProcessOnQueue(); finishReady(.failure(error))
            }
        }
    }

    private func callOnQueue(method: String, params: [String: JSONValue], timeout: TimeInterval = BridgeProtocol.requestTimeout, completion: @escaping (Result<JSONValue, BridgeError>) -> Void) {
        guard ready || method == "hello" else { completion(.failure(BridgeError(code: "not_connected", message: "Harbor runtime is not connected."))); return }
        guard process?.isRunning == true else { completion(.failure(BridgeError(code: "not_running", message: "Harbor runtime is not running."))); return }
        let request: BridgeRequest
        let data: Data
        do {
            request = try BridgeProtocol.makeRequest(method: method, params: params)
            data = try BridgeProtocol.encodeRequest(request)
        } catch {
            completion(.failure(BridgeError(code: "invalid_request", message: "The Harbor request was invalid."))); return
        }
        let timeoutWork = DispatchWorkItem { [weak self] in self?.timeout(request.id) }
        pending[request.id] = PendingRequest(method: method, timeout: timeoutWork, completion: completion)
        do {
            try input?.write(contentsOf: data)
        } catch {
            pending.removeValue(forKey: request.id)?.timeout.cancel()
            completion(.failure(BridgeError(code: "write_failed", message: "Harbor runtime stopped accepting requests.")))
            return
        }
        ioQueue.asyncAfter(deadline: .now() + timeout, execute: timeoutWork)
    }

    private func consume(_ data: Data, generation: Int) {
        guard generation == self.generation else { return }
        inputBuffer.append(contentsOf: data)
        while let newline = inputBuffer.firstIndex(of: 10) {
            let line = Array(inputBuffer[..<newline])
            inputBuffer.removeFirst(newline + 1)
            guard !line.isEmpty, line.count <= BridgeProtocol.maxLineBytes else { protocolFailure(BridgeProtocolError.inputLimit); return }
            do { try handle(BridgeProtocol.decodeResponse(Data(line))) }
            catch { protocolFailure(error as? BridgeProtocolError ?? .invalidResponse); return }
        }
        if inputBuffer.count > BridgeProtocol.maxLineBytes { protocolFailure(.inputLimit) }
    }

    private func handle(_ response: BridgeResponse) throws {
        guard let request = pending.removeValue(forKey: response.id) else { throw BridgeProtocolError.invalidResponse }
        request.timeout.cancel()
        if handshaking && request.method != "hello" { throw BridgeProtocolError.incompatibleProtocol }
        if response.ok {
            request.completion(.success(response.result ?? .null))
        } else if let error = response.error {
            request.completion(.failure(BridgeError(code: error.code, message: error.message)))
        } else {
            throw BridgeProtocolError.invalidResponse
        }
    }

    private func timeout(_ id: String) {
        guard let request = pending.removeValue(forKey: id) else { return }
        request.completion(.failure(BridgeError(code: "timeout", message: "Harbor runtime request timed out.")))
    }

    private func protocolFailure(_ error: BridgeProtocolError) {
        let message = error == .inputLimit ? "Harbor runtime sent an oversized protocol line." : "Harbor runtime sent an invalid protocol response."
        report(message)
        let failure = BridgeError(code: "protocol_error", message: message)
        failPending(failure); stopProcessOnQueue(); finishReady(.failure(failure))
    }

    private func processEOF(generation: Int) {
        guard generation == self.generation, process != nil, !process!.isRunning else { return }
        processTerminated(process!, generation: generation)
    }

    private func processTerminated(_ process: Process, generation: Int) {
        guard generation == self.generation else { return }
        self.process = nil; ready = false; handshaking = false
        output?.readabilityHandler = nil; errorOutput?.readabilityHandler = nil
        let error = BridgeError(code: "runtime_exited", message: "Harbor runtime exited.")
        failPending(error); publishConnection(false)
        if !readyWaiters.isEmpty { finishReady(.failure(error)) }
    }

    private func stopProcessOnQueue() {
        generation += 1
        let child = process
        process = nil; ready = false; handshaking = false
        output?.readabilityHandler = nil; errorOutput?.readabilityHandler = nil
        input = nil; output = nil; errorOutput = nil; inputBuffer.removeAll()
        failPending(BridgeError(code: "bridge_stopped", message: "Harbor runtime was stopped."))
        publishConnection(false)
        if let child, child.isRunning {
            let exited = DispatchSemaphore(value: 0)
            child.terminationHandler = { _ in exited.signal() }
            child.terminate()
            if exited.wait(timeout: .now() + 2) == .timedOut, child.isRunning {
                kill(child.processIdentifier, SIGKILL)
                _ = exited.wait(timeout: .now() + 1)
            }
            child.terminationHandler = nil
        }
    }

    private func failPending(_ error: BridgeError) {
        let requests = pending.values
        pending.removeAll()
        for request in requests { request.timeout.cancel(); request.completion(.failure(error)) }
    }

    private func finishReady(_ result: Result<Void, BridgeError>) {
        let waiters = readyWaiters
        readyWaiters.removeAll()
        for waiter in waiters { deliver(waiter, result) }
    }

    private func deliver<T>(_ completion: @escaping (Result<T, BridgeError>) -> Void, _ result: Result<T, BridgeError>) {
        DispatchQueue.main.async { completion(result) }
    }

    private func publish(_ snapshot: HarborStatusSnapshot) {
        DispatchQueue.main.async { [weak self] in self?.onSnapshot?(snapshot) }
    }

    private func publishConnection(_ value: Bool) {
        DispatchQueue.main.async { [weak self] in
            self?.connected = value
            self?.onConnectionChanged?(value)
        }
    }

    private func publishRuntimeVersion(_ value: String) {
        DispatchQueue.main.async { [weak self] in self?.runtimeVersion = value }
    }

    private func report(_ message: String) {
        DispatchQueue.main.async { [weak self] in self?.onError?(message) }
    }
}
