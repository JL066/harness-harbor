import SwiftUI
import UniformTypeIdentifiers
import AppKit

func statusColor(_ state: String) -> Color {
    switch state.lowercased() {
    case "healthy", "running": return .green
    case "starting", "stopping", "restarting", "partial", "warning": return .orange
    case "failed": return .red
    default: return .secondary
    }
}

private func prettyName(_ value: String) -> String {
    switch value.lowercased() {
    case "mcp": return "Harbor MCP"
    case "daemon": return "Job Daemon"
    case "codex": return "Codex CLI"
    case "agy": return "Antigravity / AGY"
    case "minimax": return "MiniMax CLI"
    case "partial": return "Partial availability"
    default: return value.replacingOccurrences(of: "_", with: " ").split(separator: " ").map { $0.capitalized }.joined(separator: " ")
    }
}

struct MenuBarView: View {
    @ObservedObject var model: HarborModel
    let openDashboard: () -> Void
    @State private var confirmQuit = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Harbor: \(prettyName(model.status.state))", systemImage: "circle.fill")
                .foregroundStyle(statusColor(model.status.state))
            ForEach(["tunnel", "mcp", "daemon"], id: \.self) { name in
                Text("\(prettyName(name)): \(prettyName(model.status.components[name]?.state ?? "stopped"))")
            }
            ForEach(model.harnesses) { harness in Text("\(prettyName(harness.name)): \(harness.status)") }
            Divider()
            Button("Open Dashboard", action: openDashboard)
            Button("Start Harbor") { model.startHarbor() }.disabled(model.busy)
            Button("Stop Harbor") { model.stopHarbor() }.disabled(model.busy)
            Button("Restart Harbor") { model.restartHarbor() }.disabled(model.busy)
            if #available(macOS 14.0, *) {
                SettingsLink {
                    Label("Settings…", systemImage: "gearshape")
                }
            } else {
                Button("Settings…") { NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil) }
            }
            Divider()
            Button("Quit") { confirmQuit = true }
        }
        .padding(10)
        .confirmationDialog("Stop Harbor and quit?", isPresented: $confirmQuit, titleVisibility: .visible) {
            Button("Stop Harbor & Quit", role: .destructive) { model.stopThenQuit() }
            Button("Cancel", role: .cancel) { }
        }
    }
}

struct MainWindowView: View {
    @ObservedObject var model: HarborModel
    @State private var showDashboard = false

    var body: some View {
        Group {
            if model.setupRequired && !showDashboard { SetupWizardView(model: model) }
            else { DashboardView(model: model) }
        }
        .frame(minWidth: 820, minHeight: 600)
        .toolbar {
            if model.setupRequired {
                Button(showDashboard ? "Setup" : "Dashboard") { showDashboard.toggle() }
            }
        }
    }
}

struct DashboardView: View {
    @ObservedObject var model: HarborModel
    @State private var logComponent = "runtime"
    @State private var testMessage = ""

    private let logComponents = ["runtime", "daemon", "tunnel"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Lighthouse · Harness Harbor").font(.largeTitle.bold())
                        Text("macOS Control Panel").foregroundStyle(.secondary)
                        Label(prettyName(model.status.state), systemImage: "circle.fill")
                            .foregroundStyle(statusColor(model.status.state))
                    }
                    Spacer()
                    if model.busy { ProgressView().controlSize(.small) }
                    Button("Start") { model.startHarbor() }.disabled(model.busy)
                    Button("Stop") { model.stopHarbor() }.disabled(model.busy)
                    Button("Restart") { model.restartHarbor() }.disabled(model.busy)
                }

                if model.status.setupRequired {
                    Text("Setup needs attention. Check Connection and local credential file settings before starting Harbor.")
                        .foregroundStyle(.orange)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                }
                if let error = model.lastError {
                    Text(error).foregroundStyle(.red).textSelection(.enabled)
                }
                componentGrid
                harnesses
                logs
                diagnostics
            }
            .padding(24)
        }
        .navigationTitle("Dashboard")
        .onAppear { model.refresh() }
    }

    private var componentGrid: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 12)], spacing: 12) {
            ForEach(["tunnel", "mcp", "daemon"], id: \.self) { key in
                let component = model.status.components[key] ?? HarborComponent(name: key)
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text(prettyName(key)).font(.headline)
                        Spacer()
                        Circle().fill(statusColor(component.state)).frame(width: 10, height: 10)
                    }
                    Text(prettyName(component.state)).foregroundStyle(statusColor(component.state))
                    Text(component.message.isEmpty ? "No additional message" : component.message)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private var harnesses: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Supported harnesses").font(.title2.bold())
                Spacer()
                Button("Refresh") { model.refresh() }
            }
            if model.harnesses.isEmpty {
                Text("Waiting for Core telemetry…").foregroundStyle(.secondary)
            } else {
                ForEach(model.harnesses) { harness in
                    HStack {
                        Circle().fill(harness.available ? .green : .secondary).frame(width: 8, height: 8)
                        Text(prettyName(harness.name)).font(.headline)
                        Text(harness.status).foregroundStyle(.secondary)
                        if !harness.summary.isEmpty { Text(harness.summary).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                        Spacer()
                        if let runningJobs = harness.runningJobsLabel {
                            Text(runningJobs).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }
                    Divider()
                }
                if !model.telemetry.agyModels.isEmpty {
                    Text("AGY models: \(model.telemetry.agyModels.joined(separator: ", "))")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
        }
    }

    private var logs: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Logs").font(.title2.bold())
                Picker("Component", selection: $logComponent) { ForEach(logComponents, id: \.self) { Text($0.capitalized).tag($0) } }
                    .labelsHidden().frame(width: 130)
                Button("Tail") { model.tailLogs(component: logComponent) }
            }
            ScrollView {
                Text(model.logText.isEmpty ? "No log output." : model.logText)
                    .font(.system(.body, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(10)
            }
            .frame(minHeight: 120, maxHeight: 220)
            .background(.black.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Diagnostics").font(.title2.bold())
                Button("Run doctor") { model.runDiagnostics() }
                Button("Export…") {
                    let panel = NSSavePanel()
                    panel.allowedContentTypes = [.json]
                    panel.nameFieldStringValue = "Harbor-diagnostics.json"
                    if panel.runModal() == .OK, let url = panel.url {
                        do { try model.diagnosticsText.write(to: url, atomically: true, encoding: .utf8) }
                        catch { model.lastError = "Diagnostics could not be exported." }
                    }
                }.disabled(model.diagnosticsText.isEmpty)
                Button("Test tunnel") {
                    model.testConnection { result in
                        switch result {
                        case .success(let value): testMessage = value.message
                        case .failure(let error): testMessage = error.message
                        }
                    }
                }
                if !testMessage.isEmpty { Text(testMessage).font(.caption).foregroundStyle(.secondary) }
            }
            if model.diagnosticsText.isEmpty {
                Text("Run doctor to inspect safe runtime diagnostics.").foregroundStyle(.secondary)
            } else {
                ScrollView {
                    Text(model.diagnosticsText)
                        .font(.system(.body, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                        .padding(10)
                }
                .frame(minHeight: 100, maxHeight: 220)
                .background(.black.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
            }
        }
    }
}

struct ConnectionFields: View {
    @Binding var draft: HarborSettings
    @Binding var secret: String
    @Binding var deleteSecret: Bool
    let configured: Bool
    var allowRemoval = true

    var body: some View {
        Section("Connection") {
            TextField("Tunnel ID", text: $draft.connection.tunnelID)
            TextField("Base URL (OpenAI default)", text: $draft.connection.baseURL)
            TextField("Profile name", text: $draft.connection.profileName)
            HStack {
                SecureField("Tunnel Runtime Key — leave blank to keep", text: $secret)
                Text(configured ? "Configured" : "Not configured").font(.caption).foregroundStyle(.secondary)
            }
            if allowRemoval { Toggle("Delete stored Tunnel Runtime Key", isOn: $deleteSecret) }
        }
    }
}

struct CodexRouteFields: View {
    @Binding var draft: HarborSettings
    @Binding var secret: String
    @Binding var deleteSecret: Bool
    let configured: Bool
    var allowRemoval = true

    private var selectedRoute: HarborRoute { HarborRoute(rawValue: draft.codex.routingMode) ?? .current }
    private var needsCustom: Bool { selectedRoute == .custom || selectedRoute == .officialThenCustom }

    var body: some View {
        Section("Codex routing") {
            Picker("Route", selection: $draft.codex.routingMode) {
                ForEach(HarborRoute.allCases) { route in Text(route.title).tag(route.rawValue) }
            }
            .onChange(of: draft.codex.routingMode) { value in
                draft.codex.custom.enabled = value == HarborRoute.custom.rawValue || value == HarborRoute.officialThenCustom.rawValue
            }
            if needsCustom || draft.codex.custom.enabled {
                TextField("Custom profile name", text: $draft.codex.custom.profileName)
                TextField("Custom base URL", text: $draft.codex.custom.baseURL)
                TextField("Default model (optional)", text: $draft.codex.custom.defaultModel)
                HStack {
                    SecureField("Custom API key — leave blank to keep", text: $secret)
                    Text(configured ? "Configured" : "Not configured").font(.caption).foregroundStyle(.secondary)
                }
                if allowRemoval { Toggle("Delete stored Custom API key", isOn: $deleteSecret) }
            }
        }
    }
}

struct ExecutableSettings: View {
    @ObservedObject var model: HarborModel
    @Binding var executables: [String: String]
    @State private var valid: [String: Bool] = [:]
    @State private var selectedKey = ""
    @State private var showImporter = false
    @State private var message = ""

    var body: some View {
        Section("Executable paths") {
            Text("Harbor runs CLI tasks only. MiniMax uses mcode. Auto Detect All finds coding CLIs and Tunnel; Save applies the paths.")
                .font(.caption).foregroundStyle(.secondary)
            Button("Auto Detect All") { autoDetect(MacSettings.executableKeys) }
            ForEach(MacSettings.executableKeys, id: \.self) { key in
                HStack {
                    Text(key == "minimax" ? "MiniMax" : key == "agy" ? "AGY" : key.capitalized).frame(width: 72, alignment: .leading)
                    TextField("Not configured", text: pathBinding(key))
                        .font(.system(.body, design: .monospaced))
                    Image(systemName: valid[key] == true ? "checkmark.circle.fill" : "questionmark.circle")
                        .foregroundStyle(valid[key] == true ? .green : .secondary)
                    Button("Choose…") { selectedKey = key; showImporter = true }
                    Button("Auto Detect") { autoDetect([key]) }
                }
            }
            HStack {
                Button("Recheck paths") { recheck() }
                if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.item], allowsMultipleSelection: false) { result in
            defer { selectedKey = "" }
            switch result {
            case .success(let urls):
                guard let url = urls.first, url.isFileURL, FileManager.default.isExecutableFile(atPath: url.path) else { message = "Selected file is not executable."; return }
                executables[selectedKey] = url.standardizedFileURL.path
                recheck()
            case .failure: message = "Executable selection was cancelled or unavailable."
            }
        }
        .onAppear { recheck() }
    }

    private func pathBinding(_ key: String) -> Binding<String> {
        Binding(get: { executables[key, default: ""] }, set: { executables[key] = $0; valid[key] = false })
    }

    private func autoDetect(_ keys: [String]) {
        model.detectExecutables { found in
            for key in keys {
                if let path = found[key], !path.isEmpty { executables[key] = path }
            }
            recheck()
            let missing = keys.filter { found[$0]?.isEmpty != false }
            message = missing.isEmpty ? "Detected all selected CLIs. Save to apply." : "Not detected: \(missing.joined(separator: ", ")). Existing paths kept."
        }
    }

    private func recheck() {
        valid = Dictionary(uniqueKeysWithValues: MacSettings.executableKeys.map { ($0, isValid(executables[$0] ?? "")) })
        message = ""
    }

    private func isValid(_ path: String) -> Bool { !path.isEmpty && path.hasPrefix("/") && FileManager.default.isExecutableFile(atPath: path) }
}

struct SettingsView: View {
    @ObservedObject var model: HarborModel
    @State private var draft: HarborSettings
    @State private var tunnelSecret = ""
    @State private var customSecret = ""
    @State private var deleteTunnel = false
    @State private var deleteCustom = false
    @State private var message = ""

    init(model: HarborModel) { self.model = model; _draft = State(initialValue: model.settings) }

    var body: some View {
        Form {
            Section("Preferences") {
                Toggle("Launch Harbor at login", isOn: Binding(get: { model.launchAtLogin }, set: { model.setLaunchAtLogin($0) }))
                Toggle("Start Harbor automatically when the app launches", isOn: $draft.macos.startAtLaunch)
                Toggle("Open Dashboard on launch", isOn: $draft.macos.openDashboard)
            }
            ConnectionFields(draft: $draft, secret: $tunnelSecret, deleteSecret: $deleteTunnel, configured: model.tunnelCredentialConfigured)
            CodexRouteFields(draft: $draft, secret: $customSecret, deleteSecret: $deleteCustom, configured: model.customCredentialConfigured)
            ExecutableSettings(model: model, executables: $draft.macos.executables)
            Section {
                HStack {
                    Button("Save Settings") { save() }.keyboardShortcut(.defaultAction).disabled(model.busy)
                    if model.busy { ProgressView().controlSize(.small) }
                    if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.secondary) }
                }
                Text("Keys are stored in private local files, separately from settings.json. Only save a custom key if you use a custom provider.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Saving restarts Harbor and stops active jobs.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Runtime directories") {
                LabeledContent("State", value: model.status.paths.state)
                LabeledContent("Jobs", value: model.status.paths.jobs)
                LabeledContent("Logs", value: model.status.paths.logs)
                Button("Test saved connection") {
                    model.testConnection { result in
                        switch result {
                        case .success(let value): message = value.message
                        case .failure(let error): message = error.message
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(minWidth: 680, minHeight: 620)
        .navigationTitle("Settings")
        .onAppear { draft = model.settings; model.refreshCredentialState() }
    }

    private func save() {
        message = ""
        model.applySettings(draft, tunnelSecret: tunnelSecret.isEmpty ? nil : tunnelSecret, customSecret: customSecret.isEmpty ? nil : customSecret, deleteTunnel: deleteTunnel, deleteCustom: deleteCustom, requireConnection: model.settings.macos.setupComplete) { result in
            switch result {
            case .success: message = "Saved. Runtime restarted."; tunnelSecret = ""; customSecret = ""; deleteTunnel = false; deleteCustom = false
            case .failure(let error): message = error.localizedDescription
            }
        }
    }
}

struct SetupWizardView: View {
    @ObservedObject var model: HarborModel
    @State private var step = 0
    @State private var draft: HarborSettings
    @State private var tunnelSecret = ""
    @State private var customSecret = ""
    @State private var deleteTunnel = false
    @State private var deleteCustom = false
    @State private var message = ""
    @State private var testPassed = false
    @State private var testMessage = ""
    @State private var startNow = true

    private let titles = ["Welcome", "Runtime", "Harnesses", "Connection", "Codex route", "Test", "Finish"]

    init(model: HarborModel) { self.model = model; _draft = State(initialValue: model.settings) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Harness Harbor setup").font(.largeTitle.bold())
                    Text("Step \(step + 1) of 7 · \(titles[step])").foregroundStyle(.secondary)
                }
                Spacer()
                ProgressView(value: Double(step + 1), total: 7).frame(width: 180)
            }
            Divider()
            ScrollView { stepView.frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, 8) }
            if let error = model.lastError { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            if !message.isEmpty { Text(message).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Button("Back") { message = ""; step -= 1 }.disabled(step == 0 || model.busy)
                Spacer()
                if step < 6 { Button("Next") { next() }.keyboardShortcut(.defaultAction).disabled(model.busy) }
                else { Button("Finish") { finish() }.keyboardShortcut(.defaultAction).disabled(model.busy || !testPassed) }
            }
        }
        .padding(28)
        .frame(minWidth: 760, minHeight: 580)
        .onAppear { model.refreshCredentialState(); model.refresh() }
        .onChange(of: draft.connection) { _ in testPassed = false }
        .onChange(of: draft.codex) { _ in testPassed = false }
        .onChange(of: draft.macos.executables) { _ in testPassed = false }
        .onChange(of: tunnelSecret) { _ in testPassed = false }
        .onChange(of: customSecret) { _ in testPassed = false }
    }

    @ViewBuilder private var stepView: some View {
        switch step {
        case 0:
            VStack(alignment: .leading, spacing: 12) {
                Text("A small control surface for Harbor’s tunnel, MCP server, job daemon, and supported harnesses.")
                Text("Install and sign in to your coding CLIs before starting work. Harbor runs tasks assigned by your supervisor.")
                Text("This wizard stores connection preferences in settings.json and saves keys in private local files only when you provide them. Existing Keychain keys are not imported; re-enter them once.").foregroundStyle(.secondary)
            }
        case 1:
            VStack(alignment: .leading, spacing: 12) {
                Text("Harbor runtime").font(.title2.bold())
                Label(model.runtimePath, systemImage: model.connected ? "checkmark.circle.fill" : "circle.dashed")
                    .foregroundStyle(model.connected ? .green : .secondary)
                    .textSelection(.enabled)
                Text("Runtime \(model.bridge.runtimeVersion.isEmpty ? "checking…" : model.bridge.runtimeVersion) · Protocol 1").font(.caption).foregroundStyle(.secondary)
                ExecutableSettings(model: model, executables: $draft.macos.executables)
            }
        case 2:
            VStack(alignment: .leading, spacing: 12) {
                HStack { Text("Core harnesses").font(.title2.bold()); Spacer(); Button("Refresh telemetry") { model.refresh() } }
                if model.harnesses.isEmpty { Text("Core telemetry is loading…").foregroundStyle(.secondary) }
                ForEach(model.harnesses) { harness in
                    Label("\(prettyName(harness.name)) — \(harness.status)", systemImage: harness.available ? "checkmark.circle.fill" : "circle")
                    if !harness.summary.isEmpty { Text(harness.summary).font(.caption).foregroundStyle(.secondary).padding(.leading, 25) }
                }
                if !model.telemetry.agyModels.isEmpty { Text("AGY models: \(model.telemetry.agyModels.joined(separator: ", "))").font(.caption).foregroundStyle(.secondary) }
            }
        case 3:
            Form { ConnectionFields(draft: $draft, secret: $tunnelSecret, deleteSecret: $deleteTunnel, configured: model.tunnelCredentialConfigured, allowRemoval: false) }.formStyle(.grouped)
        case 4:
            Form { CodexRouteFields(draft: $draft, secret: $customSecret, deleteSecret: $deleteCustom, configured: model.customCredentialConfigured, allowRemoval: false) }.formStyle(.grouped)
        case 5:
            VStack(alignment: .leading, spacing: 14) {
                Text("Test connection settings").font(.title2.bold())
                Text("Harbor will validate the settings, local credential file credential, tunnel executable, and runtime configuration before finishing setup.").foregroundStyle(.secondary)
                Button("Run connection test") { runTest() }.disabled(model.busy)
                if model.busy { ProgressView() }
                if !testMessage.isEmpty { Text(testMessage).foregroundStyle(testPassed ? .green : .orange) }
                if testPassed { Label("Connection checks passed", systemImage: "checkmark.circle.fill").foregroundStyle(.green) }
            }
        default:
            VStack(alignment: .leading, spacing: 12) {
                Text("Ready to use Harbor").font(.title2.bold())
                Text("Finish saves setup completion. You can change every setting later from the menu bar.").foregroundStyle(.secondary)
                if testPassed { Label("Connection test passed", systemImage: "checkmark.circle.fill").foregroundStyle(.green) }
                Divider()
                Toggle("Start Harbor automatically when the app launches", isOn: $draft.macos.startAtLaunch)
                Toggle("Launch Harbor at login", isOn: Binding(get: { model.launchAtLogin }, set: { model.setLaunchAtLogin($0) }))
                Toggle("Start Harbor after Finish", isOn: $startNow)
            }
        }
    }

    private func next() {
        message = ""
        if step == 3 || step == 4 {
            do { try SettingsValidator.validate(draft, requireConnection: true) }
            catch { message = error.localizedDescription; return }
        }
        step += 1
    }

    private func runTest() {
        message = ""
        testPassed = false
        testMessage = "Checking draft without changing saved settings…"
        model.testDraft(draft, tunnelSecret: tunnelSecret, customSecret: customSecret) { result in
            switch result {
            case .failure(let error): testMessage = error.localizedDescription; return
            case .success(let value): testPassed = value.ok; testMessage = value.message
            }
        }
    }

    private func finish() {
        message = ""
        model.applySettings(draft, tunnelSecret: tunnelSecret.isEmpty ? nil : tunnelSecret,
                            customSecret: customSecret.isEmpty ? nil : customSecret,
                            setupComplete: true, requireConnection: true) { result in
            switch result {
            case .success:
                tunnelSecret = ""; customSecret = ""
                if startNow { model.startHarbor() }
            case .failure(let error): message = error.localizedDescription
            }
        }
    }
}
