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
    private let popover = NSPopover()
    private let panel = DisplayPanel()
    private var policy = ConnectionPolicy()
    private var wakeActivity: NSObjectProtocol?
    private var lastVideoHealthy = ProcessInfo.processInfo.systemUptime
    private var display: CGVirtualDisplay?
    private var helper: Process?
    private var helperInput: Pipe?
    private var helperOutput: Pipe?
    private var host: NSRunningApplication?
    private var state = State.idle
    private var generation = 0
    private var errorMessage: String?
    private var pendingQuit = false
    private var quitWhenStopped = false
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
        item.button?.target = self
        item.button?.action = #selector(showControls)
        item.button?.setAccessibilityHelp("Show displays and connection controls.")
        popover.behavior = .transient
        popover.contentViewController = panel
        panel.onToggle = { [weak self] in
            guard let self else { return }
            if self.policy.requested { self.disconnect() } else { self.connect() }
        }
        panel.onSettings = {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Displays-Settings.extension")!)
        }
        panel.onQuit = { [weak self] in self?.quit() }
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil)
        updateButtons()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.checkHealth() }
        let saved = (try? Data(contentsOf: base.appendingPathComponent("connection-intent.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Bool] }
        if saved?["connect"] == true || (saved == nil && CommandLine.arguments.contains("--connect")) { connect() }
        if CommandLine.arguments.contains("--show-controls") { showControls() }
    }

    @objc private func screensChanged() { updateButtons() }

    private func updateButtons() {
        guard item != nil else { return }
        item.button?.toolTip = "Wireless display: \(info.title)"
        item.button?.setAccessibilityValue(info.title)
        // Do not redraw a closed popover on every telemetry sample.
        guard popover.isShown else { return }
        updatePanel()
    }

    private func updatePanel() {
        var screens: [PanelScreen] = []
        for screen in NSScreen.screens {
            let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
            let id: CGDirectDisplayID = number?.uint32Value ?? 0
            let wireless: Bool = display.map { $0.displayID == id } ?? false
            let pixels = String(CGDisplayPixelsWide(id)) + " × " + String(CGDisplayPixelsHigh(id))
            screens.append(PanelScreen(id: id, name: screen.localizedName, bounds: CGDisplayBounds(id),
                pixels: pixels, builtin: CGDisplayIsBuiltin(id) != 0, wireless: wireless))
        }
        screens.sort {
            if $0.bounds.minY == $1.bounds.minY { return $0.bounds.minX < $1.bounds.minX }
            return $0.bounds.minY < $1.bounds.minY
        }
        let retrying = policy.requested && (state == .idle || state == .stopping)
        let title = retrying ? "Reconnecting automatically" : info.title
        let detail: String
        if retrying { detail = "\(errorMessage ?? "Connection interrupted"). Retrying while this display is enabled." }
        else if state == .streaming { detail = "Auto-reconnect is on. Your display stays connected while this panel is closed." }
        else if state == .starting { detail = "Opening a secure connection to your display receiver." }
        else { detail = errorMessage ?? "Connect your wireless screen to extend this desktop." }
        panel.refresh(screens: screens, title: title, explanation: detail,
            wantsConnection: policy.requested, enabled: policy.requested || (state == .idle && !launchGate.isBusy && retiredHosts.isEmpty))
        popover.contentSize = panel.preferredContentSize
    }

    private func saveIntent() {
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: ["connect": policy.requested]) {
            try? data.write(to: base.appendingPathComponent("connection-intent.json"), options: .atomic)
        }
    }

    private func setStatus(_ title: String) {
        info.title = title
        updateButtons()
    }

    @objc func showControls() {
        if popover.isShown { popover.performClose(nil); return }
        guard let button = item.button else { return }
        updatePanel()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !popover.isShown { showControls() }
        else { NSApp.activate(ignoringOtherApps: true) }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    @objc func connect() {
        policy.request(); saveIntent()
        if wakeActivity == nil {
            wakeActivity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled],
                reason: "Keep the requested wireless display connection available")
        }
        startConnection()
    }

    private func startConnection() {
        guard state == .idle, !launchGate.isBusy, retiredHosts.isEmpty else { return }
        // Never take over the experimental host or an unrelated running bundle.
        guard NSRunningApplication.runningApplications(withBundleIdentifier: hostIdentifier).isEmpty else {
            policy.failed(now: ProcessInfo.processInfo.systemUptime)
            errorMessage = "Another display connection is active"
            setStatus(errorMessage!)
            return
        }
        guard FileManager.default.isExecutableFile(atPath: base.appendingPathComponent("h264-control").path),
              FileManager.default.fileExists(atPath: hostURL.path) else {
            disconnect()
            errorMessage = "Wireless display components are not installed"
            setStatus(errorMessage!)
            return
        }
        policy.request()
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
            let newDisplay = display == nil
            guard !newDisplay || createDisplay() else { fail(info.title); return }
            // Let WindowServer expose the new ID before ScreenCaptureKit asks for it.
            let deferred = DispatchWorkItem { [weak self] in
                guard let self, token == self.generation, self.state == .starting,
                      self.launchGate.begin(token) else { return }
                self.deferredHostLaunch = nil
                if newDisplay { self.arrange() }
                self.openHost(token: token)
            }
            deferredHostLaunch = deferred
            DispatchQueue.main.asyncAfter(deadline: .now() + 1, execute: deferred)
        case "status":
            guard let receiver = event["receiver"] as? [String: Any] else { return }
            // The helper verifies changing heartbeat tokens with its monotonic clock.
            // No cross-machine wall-clock comparison, and no idle frame deadline.
            lastReceiverUpdate = Date()
            let connected = receiver["connected"] as? Bool ?? false
            let frames = (receiver["written_frames"] as? NSNumber)?.intValue ?? 0
            let mpv = receiver["mpv"] as? [String: Any] ?? [:]
            if connected && frames > 0 && mpv["video-params"] is [String: Any],
               mpv["vo-configured"] as? Bool == true, host != nil {
                state = .streaming
                lastVideoHealthy = ProcessInfo.processInfo.systemUptime
                policy.healthy()
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
        completeRequestedQuit()
        if state == .idle && policy.ready(now: ProcessInfo.processInfo.systemUptime) {
            startConnection()
        }
        guard state == .starting || state == .streaming else { return }
        if let host, host.isTerminated {
            fail("The display host closed; check Screen Recording permission")
        } else if state == .starting && Date() > launchDeadline {
            fail("Video did not start in time; check Screen Recording permission")
        } else if state == .streaming && Date().timeIntervalSince(lastReceiverUpdate) > 12 {
            fail("The display receiver status is unavailable")
        } else if state == .streaming && ProcessInfo.processInfo.systemUptime - lastVideoHealthy > 15 {
            fail("The display renderer stopped responding")
        }
    }

    private func fail(_ message: String) {
        errorMessage = message
        policy.failed(now: ProcessInfo.processInfo.systemUptime)
        stopConnection()
    }

    @objc func disconnect() {
        policy.cancel(); saveIntent()
        if let wakeActivity { ProcessInfo.processInfo.endActivity(wakeActivity); self.wakeActivity = nil }
        errorMessage = nil
        if state == .idle { display = nil; setStatus("Disconnected"); return }
        stopConnection()
    }

    private func stopConnection() {
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
        if let stopTimer { RunLoop.main.add(stopTimer, forMode: .common) }
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
        if !policy.requested { display = nil }
        state = .idle
        generation += 1
        waitingForLateLaunch = launchGate.isBusy
        if waitingForLateLaunch {
            setStatus("Waiting for the display host to finish opening…")
        } else {
            setStatus(errorMessage ?? "Disconnected")
        }
        completeRequestedQuit()
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

    @objc func quit() {
        // Finish asynchronous connection teardown before asking AppKit to quit.
        // A terminate-later run loop can otherwise defer ordinary stop timers.
        quitWhenStopped = true
        disconnect()
        completeRequestedQuit()
    }

    private func completeRequestedQuit() {
        guard quitWhenStopped, state == .idle, !launchGate.isBusy, retiredHosts.isEmpty else { return }
        quitWhenStopped = false
        NSApp.terminate(nil)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        policy.cancel(); saveIntent()
        if let wakeActivity { ProcessInfo.processInfo.endActivity(wakeActivity); self.wakeActivity = nil }
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
