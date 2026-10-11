import Cocoa
import CoreGraphics
import Darwin

private final class EventBuffer { var data = Data() }

final class App: NSObject, NSApplicationDelegate {
    enum State { case idle, starting, streaming, stopping }
    private let base = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/mac-wireless-display")
    private let hostIdentifier = "local.mac-wireless-display.host"
    private var item: NSStatusItem!
    private let info = NSMenuItem(title: "Disconnected", action: nil, keyEquivalent: "")
    private var connectItem: NSMenuItem!
    private var disconnectItem: NSMenuItem!
    private var controlsWindow: NSWindow?
    private var controlsStatus: NSTextField?
    private var controlsConnect: NSButton?
    private var controlsDisconnect: NSButton?
    private var display: CGVirtualDisplay?
    private var helper: Process?
    private var helperInput: Pipe?
    private var helperOutput: Pipe?
    private var host: NSRunningApplication?
    private var state = State.idle
    private var generation = 0
    private var errorMessage: String?
    private var pendingQuit = false
    private let launchGate = HostLaunchGate()
    private var deferredHostLaunch: DispatchWorkItem?
    private var retiredHosts: [(application: NSRunningApplication, deadline: Date)] = []
    private var waitingForLateLaunch = false
    private var stopTimer: Timer?
    private var healthTimer: Timer?
    private var stopDeadline = Date.distantFuture
    private var launchDeadline = Date.distantFuture
    private var lastReceiverUpdate = Date.distantPast
    private let outputQueue = DispatchQueue(label: "wireless-display.menu.events")

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let symbol = NSImage(systemSymbolName: "display.2", accessibilityDescription: "Wireless display")?
            .withSymbolConfiguration(.preferringMonochrome())
        // Let AppKit choose contrast for the actual menu-bar appearance,
        // including highlighted, light and dark backgrounds.
        symbol?.isTemplate = true
        item.button?.image = symbol
        item.button?.contentTintColor = nil
        item.button?.setAccessibilityIdentifier("wireless-display-status")
        item.button?.setAccessibilityLabel("Wireless display")
        item.button?.setAccessibilityHelp("Open the wireless display connection menu.")
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.addItem(info)
        menu.addItem(.separator())
        connectItem = add("Connect", #selector(connect), to: menu)
        disconnectItem = add("Disconnect", #selector(disconnect), to: menu)
        _ = add("Connection status…", #selector(showControls), to: menu)
        _ = add("Arrange displays", #selector(arrange), to: menu)
        _ = add("Quit", #selector(quit), to: menu)
        item.menu = menu
        updateButtons()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.checkHealth() }
        if CommandLine.arguments.contains("--connect") { connect() }
        if CommandLine.arguments.contains("--show-controls") { showControls() }
    }

    private func add(_ title: String, _ action: Selector, to menu: NSMenu) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self
        menu.addItem(entry)
        return entry
    }

    private func updateButtons() {
        connectItem?.isEnabled = state == .idle && !launchGate.isBusy && retiredHosts.isEmpty
        disconnectItem?.isEnabled = state == .starting || state == .streaming
        controlsConnect?.isEnabled = connectItem?.isEnabled ?? false
        controlsDisconnect?.isEnabled = disconnectItem?.isEnabled ?? false
        controlsStatus?.stringValue = info.title
        item.button?.toolTip = "Wireless display: \(info.title)"
        item.button?.setAccessibilityValue(info.title)
    }

    private func setStatus(_ title: String) {
        info.title = title
        updateButtons()
    }

    @objc func showControls() {
        if controlsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 210),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Wireless Display"
            window.isReleasedWhenClosed = false
            guard let content = window.contentView else { return }
            let title = NSTextField(labelWithString: "Wireless second display")
            title.font = .systemFont(ofSize: 17, weight: .semibold)
            title.frame = NSRect(x: 24, y: 162, width: 392, height: 24)
            content.addSubview(title)
            let status = NSTextField(wrappingLabelWithString: info.title)
            status.frame = NSRect(x: 24, y: 98, width: 392, height: 52)
            status.setAccessibilityIdentifier("connection-status")
            content.addSubview(status)
            let connect = NSButton(title: "Connect", target: self, action: #selector(connect))
            connect.bezelStyle = .rounded
            connect.frame = NSRect(x: 220, y: 49, width: 90, height: 32)
            connect.setAccessibilityIdentifier("connect-display")
            content.addSubview(connect)
            let disconnect = NSButton(title: "Disconnect", target: self, action: #selector(disconnect))
            disconnect.bezelStyle = .rounded
            disconnect.frame = NSRect(x: 318, y: 49, width: 98, height: 32)
            disconnect.setAccessibilityIdentifier("disconnect-display")
            content.addSubview(disconnect)
            let note = NSTextField(labelWithString: "Closing this window keeps the connection active.")
            note.font = .systemFont(ofSize: 11)
            note.textColor = .secondaryLabelColor
            note.frame = NSRect(x: 24, y: 18, width: 392, height: 18)
            content.addSubview(note)
            controlsWindow = window
            controlsStatus = status
            controlsConnect = connect
            controlsDisconnect = disconnect
        }
        updateButtons()
        guard let window = controlsWindow else { return }
        if !window.isVisible {
            let builtin = NSScreen.screens.first {
                guard let id = $0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { return false }
                return CGDisplayIsBuiltin(id.uint32Value) != 0
            }
            if let screen = builtin ?? NSScreen.main {
                let frame = screen.visibleFrame
                window.setFrameOrigin(NSPoint(x: frame.midX - window.frame.width / 2,
                                              y: frame.midY - window.frame.height / 2))
            }
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showControls()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    @objc func connect() {
        guard state == .idle, !launchGate.isBusy, retiredHosts.isEmpty else { return }
        // Never take over the experimental host or an unrelated running bundle.
        guard NSRunningApplication.runningApplications(withBundleIdentifier: hostIdentifier).isEmpty else {
            setStatus("Another display connection is active; close it first")
            return
        }
        guard FileManager.default.isExecutableFile(atPath: base.appendingPathComponent("h264-control").path),
              FileManager.default.fileExists(atPath: hostURL.path) else {
            setStatus("Wireless display components are not installed")
            return
        }
        generation += 1
        let token = generation
        errorMessage = nil
        state = .starting
        setStatus("Connecting…")
        launchDeadline = Date().addingTimeInterval(35)
        lastReceiverUpdate = .distantPast
        let process = Process()
        let input = Pipe(), output = Pipe()
        let buffer = EventBuffer()
        process.executableURL = base.appendingPathComponent("h264-control")
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        helperInput = input
        helperOutput = output
        helper = process
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            self?.outputQueue.async { [weak self] in
                guard let self else { return }
                buffer.data.append(data)
                while let end = buffer.data.firstIndex(of: 10) {
                    let line = Data(buffer.data[..<end])
                    buffer.data.removeSubrange(...end)
                    if let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                        DispatchQueue.main.async { self.handle(event, token: token) }
                    }
                }
            }
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.generation == token else { return }
                if self.state != .stopping && self.state != .idle {
                    self.fail("The video connection closed")
                }
            }
        }
        do { try process.run() }
        catch { fail("The connection helper could not start") }
    }

    private var hostURL: URL { base.appendingPathComponent("build/Wireless Display Host.app") }

    private func createDisplay() -> Bool {
        let descriptor = CGVirtualDisplayDescriptor()
        descriptor.name = "Wireless Display"
        descriptor.maxPixelsWide = 1920
        descriptor.maxPixelsHigh = 1080
        descriptor.sizeInMillimeters = CGSize(width: 598, height: 336)
        descriptor.vendorID = 0x3456
        descriptor.productID = 0x2234
        descriptor.serialNum = 0x1011
        descriptor.setDispatchQueue(.main)
        let virtual = CGVirtualDisplay(descriptor: descriptor)
        let settings = CGVirtualDisplaySettings()
        settings.hiDPI = 0
        // Keep capture-cadence experiments independent of the screen-authorized host.
        // The tested default is 120 Hz; only the virtual output is changed.
        // A faster virtual refresh can improve capture cadence, but WindowServer
        // may compose more often even when the encoder is capped at 60 fps.
        var refreshRate = 120.0
        if let data = try? Data(contentsOf: base.appendingPathComponent("display-settings.json")),
           let values = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let requested = values["refreshRate"] as? Double,
           requested.isFinite, (55.0...120.0).contains(requested) {
            refreshRate = requested
        }
        settings.modes = [CGVirtualDisplayMode(width: 1920, height: 1080, refreshRate: refreshRate)]
        guard virtual.apply(settings) else {
            setStatus("The virtual display could not be created")
            return false
        }
        do {
            try String(virtual.displayID).write(to: base.appendingPathComponent("display-id"), atomically: true, encoding: .utf8)
        } catch {
            setStatus("The virtual display information could not be saved")
            return false
        }
        display = virtual
        return true
    }

    private func handle(_ event: [String: Any], token: Int) {
        guard token == generation, state == .starting || state == .streaming else { return }
        switch event["event"] as? String {
        case "starting":
            setStatus(event["message"] as? String ?? "Connecting…")
        case "ready":
            guard host == nil, launchGate.schedule(token) else { return }
            guard createDisplay() else { fail(info.title); return }
            // Let WindowServer expose the new ID before ScreenCaptureKit asks for it.
            let deferred = DispatchWorkItem { [weak self] in
                guard let self, token == self.generation, self.state == .starting,
                      self.launchGate.begin(token) else { return }
                self.deferredHostLaunch = nil
                self.arrange()
                self.openHost(token: token)
            }
            deferredHostLaunch = deferred
            DispatchQueue.main.asyncAfter(deadline: .now() + 1, execute: deferred)
        case "status":
            guard let receiver = event["receiver"] as? [String: Any] else { return }
            let formatter = ISO8601DateFormatter()
            guard let stamp = receiver["updated_at"] as? String,
                  let updated = formatter.date(from: stamp), abs(updated.timeIntervalSinceNow) < 10 else { return }
            lastReceiverUpdate = Date()
            let connected = receiver["connected"] as? Bool ?? false
            let frames = (receiver["written_frames"] as? NSNumber)?.intValue ?? 0
            let mpv = receiver["mpv"] as? [String: Any] ?? [:]
            if connected && frames > 0 && mpv["video-params"] is [String: Any],
               mpv["vo-configured"] as? Bool == true, host != nil {
                state = .streaming
                setStatus("Connected · 1080p")
            } else if state == .streaming {
                setStatus("Waiting for the video connection…")
            }
        case "error": fail(event["message"] as? String ?? "The connection could not be established")
        default: break
        }
    }

    private func openHost(token: Int) {
        guard NSRunningApplication.runningApplications(withBundleIdentifier: hostIdentifier).isEmpty else {
            launchGate.complete(token)
            fail("Another display connection is already active")
            return
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        // Each callback must identify its own launch, never a reused newer host.
        configuration.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: hostURL, configuration: configuration) { [weak self] running, error in
            DispatchQueue.main.async {
                guard let self else { running?.terminate(); return }
                self.launchGate.complete(token)
                guard token == self.generation, self.state == .starting else {
                    if let running {
                        self.retiredHosts.append((running, Date().addingTimeInterval(3)))
                        running.terminate()
                    }
                    self.updateButtons()
                    return
                }
                guard let running, error == nil else { self.fail("The display host could not open"); return }
                self.host = running
                self.send("HOST \(running.processIdentifier)")
                self.launchDeadline = Date().addingTimeInterval(15)
                self.setStatus("Waiting for video…")
            }
        }
    }

    private func send(_ line: String) {
        guard let handle = helperInput?.fileHandleForWriting else { return }
        try? handle.write(contentsOf: Data((line + "\n").utf8))
    }

    private func checkHealth() {
        retiredHosts.removeAll { $0.application.isTerminated }
        for retired in retiredHosts where Date() > retired.deadline {
            retired.application.forceTerminate()
        }
        if waitingForLateLaunch && !launchGate.isBusy && retiredHosts.isEmpty {
            waitingForLateLaunch = false
            if state == .idle {
                setStatus(errorMessage ?? "Disconnected")
            }
        }
        updateButtons()
        guard state == .starting || state == .streaming else { return }
        if let host, host.isTerminated {
            fail("The display host closed; check Screen Recording permission")
        } else if state == .starting && Date() > launchDeadline {
            fail("Video did not start in time; check Screen Recording permission")
        } else if state == .streaming && Date().timeIntervalSince(lastReceiverUpdate) > 12 {
            fail("The display receiver status is unavailable")
        }
    }

    private func fail(_ message: String) {
        errorMessage = message
        disconnect()
    }

    @objc func disconnect() {
        guard state != .idle, state != .stopping else { return }
        state = .stopping
        setStatus("Disconnecting…")
        deferredHostLaunch?.cancel()
        deferredHostLaunch = nil
        launchGate.cancelScheduled(generation)
        host?.terminate()
        send("STOP")
        try? helperInput?.fileHandleForWriting.close()
        helperInput = nil
        stopDeadline = Date().addingTimeInterval(10)
        stopTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.finishStopWhenReady() }
        finishStopWhenReady()
    }

    private func finishStopWhenReady() {
        let hostStopped = host?.isTerminated ?? true
        let helperStopped = !(helper?.isRunning ?? false)
        if launchGate.inFlight.contains(generation), Date() < stopDeadline { return }
        if !retiredHosts.isEmpty { return }
        if !hostStopped || !helperStopped {
            if Date() > stopDeadline {
                // These objects identify only processes created by this session.
                if !hostStopped { host?.forceTerminate() }
                if !helperStopped { helper?.terminate() }
                stopDeadline = Date().addingTimeInterval(3)
            }
            return
        }
        stopTimer?.invalidate()
        stopTimer = nil
        helperOutput?.fileHandleForReading.readabilityHandler = nil
        helperOutput = nil
        helper = nil
        host = nil
        display = nil
        state = .idle
        generation += 1
        waitingForLateLaunch = launchGate.isBusy
        if waitingForLateLaunch {
            setStatus("Waiting for the display host to finish opening…")
        } else {
            setStatus(errorMessage ?? "Disconnected")
        }
        if pendingQuit {
            pendingQuit = false
            // Stay available to retire a delayed OS launch instead of orphaning it.
            NSApp.reply(toApplicationShouldTerminate: !waitingForLateLaunch)
        }
    }

    @objc func arrange() {
        guard let virtual = display else { return }
        // Move only the display owned by this session. Other displays retain
        // the user's existing arrangement.
        let main = CGMainDisplayID()
        guard main != virtual.displayID else { return }
        let bounds = CGDisplayBounds(main)
        var configuration: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&configuration) == .success,
              let configuration else { return }
        let result = CGConfigureDisplayOrigin(configuration, virtual.displayID,
                                              Int32(bounds.maxX), Int32(bounds.minY))
        if result == .success { CGCompleteDisplayConfiguration(configuration, .forSession) }
        else { CGCancelDisplayConfiguration(configuration) }
    }

    @objc func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if state == .idle && (launchGate.isBusy || !retiredHosts.isEmpty) {
            setStatus("Waiting for the display host to finish opening…")
            return .terminateCancel
        }
        guard state != .idle else { return .terminateNow }
        pendingQuit = true
        disconnect()
        return .terminateLater
    }
}

@main
private enum MenuMain {
    static func main() {
        signal(SIGPIPE, SIG_IGN)
        let app = NSApplication.shared
        let delegate = App()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) { app.run() }
    }
}
