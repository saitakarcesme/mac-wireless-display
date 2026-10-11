import Cocoa
import CoreGraphics

struct PanelScreen {
    let id: CGDirectDisplayID
    let name: String
    let bounds: CGRect
    let pixels: String
    let builtin: Bool
    let wireless: Bool
}

private final class DisplayMap: NSView {
    var screens: [PanelScreen] = [] { didSet { needsDisplay = true } }
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSColor.quaternaryLabelColor.withAlphaComponent(0.15).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 12, yRadius: 12).fill()
        guard let first = screens.first else { return }
        let union = screens.dropFirst().reduce(first.bounds) { $0.union($1.bounds) }
        guard union.width > 0 && union.height > 0 else { return }
        let scale = min((bounds.width - 46) / union.width, (bounds.height - 34) / union.height)
        let offset = CGPoint(x: (bounds.width - union.width * scale) / 2,
                             y: (bounds.height - union.height * scale) / 2)
        for (index, screen) in screens.enumerated() {
            let r = CGRect(x: offset.x + (screen.bounds.minX - union.minX) * scale + 2,
                           y: offset.y + (screen.bounds.minY - union.minY) * scale + 2,
                           width: max(4, screen.bounds.width * scale - 4),
                           height: max(4, screen.bounds.height * scale - 4))
            let shape = NSBezierPath(roundedRect: r, xRadius: 5, yRadius: 5)
            (screen.wireless ? NSColor.controlAccentColor.withAlphaComponent(0.16) : NSColor.controlBackgroundColor).setFill()
            shape.fill()
            (screen.wireless ? NSColor.controlAccentColor : NSColor.separatorColor).setStroke()
            shape.lineWidth = screen.wireless ? 1.5 : 1
            shape.stroke()
            let text = "\(index + 1)"
            let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 18, weight: .medium),
                .foregroundColor: screen.wireless ? NSColor.controlAccentColor : NSColor.labelColor]
            let size = text.size(withAttributes: attrs)
            text.draw(at: CGPoint(x: r.midX - size.width / 2, y: r.midY - size.height / 2), withAttributes: attrs)
            if screen.builtin {
                NSColor.tertiaryLabelColor.setFill()
                NSBezierPath(roundedRect: CGRect(x: r.minX - 3, y: r.maxY + 1, width: r.width + 6, height: 3), xRadius: 1, yRadius: 1).fill()
            }
        }
    }
}

final class DisplayPanel: NSViewController {
    var onToggle: (() -> Void)?
    var onSettings: (() -> Void)?
    var onQuit: (() -> Void)?
    private let heading = NSTextField(labelWithString: "Displays")
    private let countLabel = NSTextField(labelWithString: "")
    private let map = DisplayMap()
    private let status = NSTextField(labelWithString: "Disconnected")
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let primary = NSButton(title: "Connect", target: nil, action: nil)
    private let settings = NSButton(title: "Display Settings…", target: nil, action: nil)
    private let quitButton = NSButton(title: "Quit", target: nil, action: nil)
    private var rowViews: [NSView] = []
    private var topology = ""

    override func loadView() {
        let root = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: 404, height: 520))
        root.material = .popover; root.blendingMode = .behindWindow; root.state = .active
        view = root
        heading.font = .systemFont(ofSize: 21, weight: .semibold)
        countLabel.font = .systemFont(ofSize: 12); countLabel.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 13, weight: .semibold)
        status.setAccessibilityIdentifier("connection-status")
        detail.font = .systemFont(ofSize: 11); detail.textColor = .secondaryLabelColor
        primary.bezelStyle = .rounded; primary.controlSize = .large
        primary.target = self; primary.action = #selector(toggle)
        primary.setAccessibilityIdentifier("toggle-display")
        settings.bezelStyle = .rounded; settings.target = self; settings.action = #selector(openSettings)
        quitButton.isBordered = false; quitButton.font = .systemFont(ofSize: 11)
        quitButton.target = self; quitButton.action = #selector(quit)
        map.setAccessibilityElement(true); map.setAccessibilityRole(.image)
        for child in [heading, countLabel, map, status, detail, primary, settings, quitButton] { root.addSubview(child) }
    }

    func refresh(screens: [PanelScreen], title: String, explanation: String, wantsConnection: Bool, enabled: Bool) {
        loadViewIfNeeded()
        let height = CGFloat(410 + max(3, screens.count) * 38)
        preferredContentSize = NSSize(width: 404, height: height)
        view.setFrameSize(preferredContentSize)
        heading.frame = NSRect(x: 22, y: height - 49, width: 220, height: 28)
        countLabel.stringValue = "\(screens.count) display\(screens.count == 1 ? "" : "s") · extended desktop"
        countLabel.frame = NSRect(x: 23, y: height - 69, width: 358, height: 17)
        map.frame = NSRect(x: 22, y: height - 255, width: 360, height: 170)
        map.screens = screens
        map.setAccessibilityLabel("Display arrangement. " + screens.enumerated().map {
            "\($0.offset + 1): \($0.element.name), x \(Int($0.element.bounds.minX)), y \(Int($0.element.bounds.minY))"
        }.joined(separator: ". "))
        let signature = screens.map { "\($0.id)|\($0.name)|\($0.bounds)|\($0.pixels)|\($0.wireless)" }.joined()
        if signature != topology {
            topology = signature
            rowViews.forEach { $0.removeFromSuperview() }; rowViews.removeAll()
            for (i, screen) in screens.enumerated() {
                let rowY = height - 294 - CGFloat(i * 38)
                let number = NSTextField(labelWithString: "\(i + 1)")
                number.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
                number.textColor = screen.wireless ? .controlAccentColor : .secondaryLabelColor
                number.frame = NSRect(x: 28, y: rowY + 7, width: 20, height: 18)
                let name = NSTextField(labelWithString: screen.name)
                name.font = .systemFont(ofSize: 12, weight: .medium)
                name.lineBreakMode = .byTruncatingTail
                name.frame = NSRect(x: 52, y: rowY + 14, width: 265, height: 18)
                let dimensions = NSTextField(labelWithString: screen.pixels + (screen.wireless ? " · Wireless" : screen.builtin ? " · Built-in" : " · Wired"))
                dimensions.font = .systemFont(ofSize: 10); dimensions.textColor = .secondaryLabelColor
                dimensions.frame = NSRect(x: 52, y: rowY, width: 300, height: 15)
                for label in [number, name, dimensions] { view.addSubview(label); rowViews.append(label) }
            }
        }
        status.stringValue = title
        status.frame = NSRect(x: 24, y: 115, width: 354, height: 20)
        detail.stringValue = explanation
        detail.frame = NSRect(x: 24, y: 69, width: 354, height: 40)
        primary.title = wantsConnection ? "Disconnect" : "Connect"
        primary.isEnabled = enabled
        primary.frame = NSRect(x: 257, y: 24, width: 126, height: 34)
        settings.frame = NSRect(x: 16, y: 26, width: 144, height: 30)
        quitButton.frame = NSRect(x: 173, y: 29, width: 50, height: 24)
    }
    @objc private func toggle() { onToggle?() }
    @objc private func openSettings() { onSettings?() }
    @objc private func quit() { onQuit?() }
}
