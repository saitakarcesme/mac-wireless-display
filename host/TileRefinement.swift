import AppKit
import CoreGraphics
import CoreVideo
import ScreenCaptureKit

enum TileGrid {
    static let columns = 8, rows = 6, count = 48
    static let width = 240, height = 180
    static let allMask: UInt64 = (1 << 48) - 1
    static let quietNS: UInt64 = 750_000_000

    static func rect(_ tile: Int) -> CGRect {
        CGRect(x: tile % columns * width, y: tile / columns * height, width: width, height: height)
    }

    /// Conservative damage in top-left output pixels. Unknown geometry or
    /// metadata invalidates everything; an empty complete frame is not assumed clean.
    static func damage(_ attachments: [SCStreamFrameInfo: Any]?) -> (mask: UInt64, fallback: Bool) {
        guard let attachments,
              let scale = attachments[.scaleFactor] as? NSNumber, abs(scale.doubleValue - 1) < 0.0001,
              let contentScale = attachments[.contentScale] as? NSNumber, abs(contentScale.doubleValue - 1) < 0.0001,
              let content = rectangle(attachments[.contentRect]),
              content == CGRect(x: 0, y: 0, width: 1920, height: 1080),
              let values = attachments[.dirtyRects] as? [Any], !values.isEmpty, values.count <= 4096 else {
            return (allMask, true)
        }
        var result: UInt64 = 0
        for value in values {
            guard let dirty = rectangle(value),
                  dirty.origin.x.isFinite, dirty.origin.y.isFinite,
                  dirty.width.isFinite, dirty.height.isFinite,
                  dirty.width >= 0, dirty.height >= 0 else { return (allMask, true) }
            if dirty.isEmpty { continue }
            let clipped = dirty.intersection(CGRect(x: 0, y: 0, width: 1920, height: 1080))
            if clipped.isNull || clipped.isEmpty { continue }
            // Outward rounding protects partially covered pixels and tile borders.
            let left = max(0, Int(floor(clipped.minX)) / width)
            let right = min(columns - 1, (Int(ceil(clipped.maxX)) - 1) / width)
            let top = max(0, Int(floor(clipped.minY)) / height)
            let bottom = min(rows - 1, (Int(ceil(clipped.maxY)) - 1) / height)
            for y in top...bottom { for x in left...right { result |= 1 << UInt64(y * columns + x) } }
        }
        return result == 0 ? (allMask, true) : (result, false)
    }

    static func rectangle(_ value: Any?) -> CGRect? {
        if let value = value as? NSValue {
            let encoding = String(cString: value.objCType)
            guard encoding.contains("CGRect") || encoding.contains("NSRect") else { return nil }
            return value.rectValue
        }
        if let value = value as? CGRect { return value }
        if let value = value as? [String: Any] { return CGRect(dictionaryRepresentation: value as CFDictionary) }
        return nil
    }

    static func changedMask(_ versions: [UInt64], since previous: [UInt64]?) -> UInt64 {
        guard versions.count == count, let previous, previous.count == count else { return allMask }
        return (0..<count).reduce(UInt64(0)) { $0 | (versions[$1] == previous[$1] ? 0 : 1 << UInt64($1)) }
    }

    static func cursorSafeMask(_ damage: UInt64, previous: CGPoint?, current: CGPoint?) -> UInt64 {
        guard let previous, let current, current.x.isFinite, current.y.isFinite,
              previous == current else { return allMask }
        return damage
    }

    /// The full snapshot already has opaque top-to-bottom BGRA rows. Copy only
    /// one tile, with a packed stride, so mpv never maps past a bottom-row offset.
    static func pixels(_ full: Data, tile: Int) throws -> Data {
        guard (0..<count).contains(tile), full.count == 1920 * 1080 * 4 else {
            throw RefinementFailure("invalid tile geometry")
        }
        var output = Data(count: width * height * 4)
        let x = tile % columns * width, y = tile / columns * height
        output.withUnsafeMutableBytes { destination in
            full.withUnsafeBytes { source in
                for row in 0..<height {
                    memcpy(destination.baseAddress!.advanced(by: row * width * 4),
                           source.baseAddress!.advanced(by: ((y + row) * 1920 + x) * 4), width * 4)
                }
            }
        }
        return output
    }
}

struct TileCandidate {
    let tile: Int
    let version: UInt64
    let sequence: UInt64
}

/// Pure state machine; its owner serializes every access with one lock.
struct TileTracker {
    private(set) var versions = [UInt64](repeating: 0, count: TileGrid.count)
    private var changedAt = [UInt64](repeating: 0, count: TileGrid.count)
    private var refined = [UInt64?](repeating: nil, count: TileGrid.count)
    private var generation: UInt64 = 0
    private var sentVersions: [UInt64]?
    private var sequence: UInt64?

    mutating func capture(mask: UInt64, at now: UInt64) -> [UInt64] {
        generation &+= 1
        for tile in 0..<TileGrid.count where mask & (1 << UInt64(tile)) != 0 {
            versions[tile] = generation
            changedAt[tile] = now
        }
        return versions
    }

    mutating func sent(_ versions: [UInt64], sequence: UInt64) {
        guard versions.count == TileGrid.count else { return }
        sentVersions = versions; self.sequence = sequence
    }

    mutating func restartTransport() {
        invalidateRefinement()
        sentVersions = nil; sequence = nil
    }

    mutating func invalidateRefinement() { refined = [UInt64?](repeating: nil, count: TileGrid.count) }

    func candidates(at now: UInt64) -> [TileCandidate] {
        guard let sentVersions, let sequence else { return [] }
        return (0..<TileGrid.count).compactMap { tile in
            guard versions[tile] == sentVersions[tile], refined[tile] != versions[tile],
                  RefinementTiming.settled(now: now, lastMotion: changedAt[tile], quiet: TileGrid.quietNS) else { return nil }
            return TileCandidate(tile: tile, version: versions[tile], sequence: sequence)
        }
    }

    func valid(_ candidate: TileCandidate) -> Bool {
        versions[candidate.tile] == candidate.version && sentVersions != nil
    }

    mutating func accepted(_ candidate: TileCandidate) {
        if valid(candidate) { refined[candidate.tile] = candidate.version }
    }
}

/// One RGB snapshot per second at most, only when some unrefined region has
/// stayed unchanged for 750ms. A blinking caret never cancels other regions.
final class TileRefinement {
    private let filter: SCContentFilter
    private let port: UInt16
    private let report: (String) -> Void
    private let queue = DispatchQueue(label: "wireless-display.tile-refinement", qos: .utility)
    private let lock = NSLock()
    private var tracker = TileTracker()
    private var working = false
    private var nextAttemptNS: UInt64 = 0
    private var transportGeneration: UInt64 = 0
    private var receiverEpoch: String?
    private var nextEpochPollNS: UInt64 = 0
    private var timer: DispatchSourceTimer?
    private var damageDiagnostics = 0
    private var lastCursorPosition: CGPoint?

    init(display: SCDisplay, port: UInt16 = 16011, report: @escaping (String) -> Void) {
        filter = SCContentFilter(display: display, excludingWindows: [])
        self.port = port; self.report = report
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + .milliseconds(800), repeating: .milliseconds(100), leeway: .milliseconds(20))
        timer.setEventHandler { [weak self] in self?.attempt() }
        self.timer = timer; timer.resume()
    }

    deinit { timer?.cancel() }

    func frameCaptured(_ attachments: [SCStreamFrameInfo: Any]?) -> [UInt64] {
        let damage = TileGrid.damage(attachments)
        // No guessed cursor-size bound: any pointer movement invalidates all
        // tiles until SCK's old/new cursor damage coverage is verified on-device.
        let cursorPosition = CGEvent(source: nil)?.location
        return lock.withLock {
            let mask = TileGrid.cursorSafeMask(damage.mask, previous: lastCursorPosition, current: cursorPosition)
            lastCursorPosition = cursorPosition
            if damageDiagnostics < 4 {
                damageDiagnostics += 1
                report("tile-damage mask=\(String(mask, radix: 16)) fallback=\(damage.fallback) cursor_invalidated=\(mask != damage.mask) dirty_type=\(String(describing: attachments?[.dirtyRects].map { type(of: $0) }))")
            }
            return tracker.capture(mask: mask, at: DispatchTime.now().uptimeNanoseconds)
        }
    }

    func frameSent(versions: [UInt64], sequence: UInt64, connectionGeneration: UInt64) {
        lock.withLock {
            if transportGeneration != connectionGeneration {
                transportGeneration = connectionGeneration
                receiverEpoch = nil
                tracker.restartTransport()
            }
            tracker.sent(versions, sequence: sequence)
        }
    }

    private func valid(_ candidate: TileCandidate, transport: UInt64) -> Bool {
        lock.withLock { transportGeneration == transport && tracker.valid(candidate) }
    }

    private func attempt() {
        let job: ([TileCandidate], UInt64)? = lock.withLock {
            let now = DispatchTime.now().uptimeNanoseconds
            guard !working, now >= nextAttemptNS, transportGeneration > 0 else { return nil }
            let candidates = tracker.candidates(at: now)
            guard !candidates.isEmpty || now >= nextEpochPollNS else { return nil }
            working = true
            if !candidates.isEmpty { nextAttemptNS = now + 1_000_000_000 }
            nextEpochPollNS = now + 2_000_000_000
            return (candidates, transportGeneration)
        }
        guard let (candidates, transport) = job else { return }
        let socket: RefinementSocket
        let epoch: String
        do {
            socket = try RefinementSocket(port: port) { [weak self] in
                guard let self else { return false }
                return self.lock.withLock {
                    self.transportGeneration == transport &&
                        (candidates.isEmpty || candidates.contains { self.tracker.valid($0) })
                }
            }
            let hello = try socket.readJSON()
            guard hello["version"] as? Int == 2, hello["kind"] as? String == "hello",
                  hello["columns"] as? Int == TileGrid.columns, hello["rows"] as? Int == TileGrid.rows,
                  hello["tile_width"] as? Int == TileGrid.width, hello["tile_height"] as? Int == TileGrid.height,
                  let value = hello["epoch"] as? String, UUID(uuidString: value) != nil else {
                throw RefinementFailure("tile receiver protocol mismatch")
            }
            epoch = value
            lock.withLock {
                if transportGeneration == transport && receiverEpoch != epoch {
                    receiverEpoch = epoch
                    tracker.invalidateRefinement()
                }
            }
        } catch { finish("tile-refinement deferred reason=\(error)", invalidateCache: true); return }
        // A cheap hello poll detects player epoch resets even while all tiles
        // remain unchanged and the underlying video SSH connection stays open.
        if candidates.isEmpty { finish(nil); return }
        guard candidates.contains(where: { valid($0, transport: transport) }) else { finish(nil); return }
        let config = SCStreamConfiguration()
        config.width = 1920; config.height = 1080
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.colorSpaceName = CGColorSpace.sRGB; config.captureDynamicRange = .SDR
        config.showsCursor = true; config.shouldBeOpaque = true
        let started = DispatchTime.now().uptimeNanoseconds
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { [weak self] image, error in
            guard let self else { return }
            self.queue.async {
                var accepted = 0, stale = 0, bytes = 0
                do {
                    guard candidates.contains(where: { self.valid($0, transport: transport) }) else {
                        throw RefinementFailure("all candidate tiles changed")
                    }
                    guard let image, error == nil else { throw RefinementFailure("RGB capture failed") }
                    let full = try RefinementPacket.bgra(image)
                    for candidate in candidates {
                        guard self.valid(candidate, transport: transport) else { stale += 1; continue }
                        let raw = try TileGrid.pixels(full, tile: candidate.tile)
                        let payload = try RefinementPacket.compress(raw)
                        guard payload.count <= 256 * 1024 else { throw RefinementFailure("tile payload exceeds limit") }
                        guard self.valid(candidate, transport: transport) else { stale += 1; continue }
                        try socket.writeJSON(["version": 2, "kind": "tile", "epoch": epoch,
                            "sequence": candidate.sequence, "tile": candidate.tile,
                            "width": TileGrid.width, "height": TileGrid.height, "stride": TileGrid.width * 4,
                            "encoding": "bgra-zlib", "payload_bytes": payload.count])
                        try socket.write(payload, rateLimitBytesPerSecond: 2_000_000)
                        let response = try socket.readJSON()
                        guard response["version"] as? Int == 2, response["kind"] as? String == "result",
                              (response["sequence"] as? NSNumber)?.uint64Value == candidate.sequence,
                              response["tile"] as? Int == candidate.tile,
                              let wasAccepted = response["accepted"] as? Bool else {
                            throw RefinementFailure("invalid tile result")
                        }
                        bytes += payload.count
                        if wasAccepted {
                            accepted += 1
                            self.lock.withLock {
                                if self.transportGeneration == transport { self.tracker.accepted(candidate) }
                            }
                        } else { stale += 1 }
                    }
                    let ms = (DispatchTime.now().uptimeNanoseconds - started) / 1_000_000
                    self.finish("tile-refinement accepted=\(accepted) stale=\(stale) compressed_bytes=\(bytes) elapsed_ms=\(ms)")
                } catch {
                    self.finish("tile-refinement deferred reason=\(error) accepted=\(accepted) stale=\(stale)", invalidateCache: true)
                }
            }
        }
    }

    private func finish(_ message: String?, invalidateCache: Bool = false) {
        lock.withLock {
            working = false
            // A partial/malformed batch may make the receiver clear every tile
            // without changing its video epoch. Forget successful cached tiles too.
            if invalidateCache { tracker.invalidateRefinement() }
        }
        if let message { report(message) }
    }
}
