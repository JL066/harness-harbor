import SwiftUI
import AppKit

@MainActor final class HarborAppDelegate: NSObject, NSApplicationDelegate {
    let model = HarborModel()
    private var dashboard: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        model.launch { [weak self] in self?.showDashboard() }
    }

    func showDashboard() {
        if dashboard == nil {
            let window = NSWindow(contentViewController: NSHostingController(rootView: MainWindowView(model: model)))
            window.title = "Harness Harbor"
            window.setContentSize(NSSize(width: 900, height: 700))
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            window.isReleasedWhenClosed = false
            window.center()
            dashboard = window
        }
        dashboard?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showDashboard()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !model.readyToQuit else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Stop Harbor and quit?"
        alert.informativeText = "Running jobs will be stopped. Saved job history will be preserved."
        alert.addButton(withTitle: "Stop Harbor & Quit")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn { model.stopThenQuit() }
        return .terminateCancel
    }
}

@main
struct HarnessHarborApp: App {
    @NSApplicationDelegateAdaptor(HarborAppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(model: delegate.model, openDashboard: delegate.showDashboard)
        } label: {
            HarborMenuLabel(model: delegate.model)
        }
        .menuBarExtraStyle(.menu)

        Settings {
            SettingsView(model: delegate.model)
        }
    }
}

private struct HarborMenuLabel: View {
    @ObservedObject var model: HarborModel
    var body: some View {
        Label("Harbor", systemImage: model.status.state.lowercased() == "healthy" ? "circle.fill" : "circle")
            .foregroundStyle(statusColor(model.status.state))
    }
}
