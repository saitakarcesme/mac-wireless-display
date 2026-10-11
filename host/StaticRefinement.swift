import Foundation
import CoreGraphics
import CoreVideo
import ScreenCaptureKit
import Darwin
import zlib

struct RefinementFailure: Error, CustomStringConvertible {
    let description: String
    init(_ text: String) { description = text }
}

enum RefinementTiming {
    static func settled(now: UInt64, lastMotion: UInt64, quiet: UInt64) -> Bool {
        now >= lastMotion && now - lastMotion >= quiet
    }
}

/// Lossless original RGB for settled content. Never derives RGB from the NV12 video.
/// All work is idle-only, cancelable when the capture generation changes, and isolated
/// from the video encoder/output queues. It uses its own authenticated SSH forward.
final class StaticRefinement {
    private let filter: SCContentFilter
    private let port: UInt16
    private let report: (String) -> Void
    private let queue = DispatchQueue(label: "wireless-display.refinement", qos: .utility)
    private let lock = NSLock()
    private var generation: UInt64 = 0
    private var lastMotionNS = DispatchTime.now().uptimeNanoseconds
    private var lastSequence: UInt64?
    private var completedIdentity: (generation: UInt64, sequence: UInt64)?
    private var working = false
    private var retryAfterNS: UInt64 = 0
    private var timer: DispatchSourceTimer?
    private let quietNS: UInt64 = 350_000_000

    init(display: SCDisplay, port: UInt16 = 16011, report: @escaping (String) -> Void) {
        filter = SCContentFilter(display: display, excludingWindows: [])
        self.port = port
        self.report = report
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + .milliseconds(400), repeating: .milliseconds(100), leeway: .milliseconds(20))
        timer.setEventHandler { [weak self] in self?.attempt() }
        self.timer = timer
        timer.resume()
    }

    deinit { timer?.cancel() }

    func frameCaptured() {
        lock.withLock {
            generation &+= 1
            lastMotionNS = DispatchTime.now().uptimeNanoseconds
        }
    }

    func frameSent(sequence: UInt64) { lock.withLock { lastSequence = sequence } }

    private func unchanged(_ token: UInt64, sequence: UInt64) -> Bool {
        lock.withLock { generation == token && lastSequence == sequence }
    }

    private func attempt() {
        let state: (UInt64, UInt64)? = lock.withLock {
            // Capture callbacks may advance lastMotionNS while this queue waits
            // for the lock. Sample the clock inside it, and never subtract a future value.
            let now = DispatchTime.now().uptimeNanoseconds
            guard !working, now >= retryAfterNS,
                  RefinementTiming.settled(now: now, lastMotion: lastMotionNS, quiet: quietNS),
                  let sequence = lastSequence,
                  completedIdentity?.generation != generation || completedIdentity?.sequence != sequence else { return nil }
            working = true
            return (generation, sequence)
        }
        guard let (token, sequence) = state else { return }
        // A new socket per settled snapshot obtains the current video epoch before
        // capturing. This also bounds resources across host/receiver reconnection.
        let socket: RefinementSocket
        let epoch: String
        do {
            socket = try RefinementSocket(port: port) { [weak self] in
                self?.unchanged(token, sequence: sequence) == true
            }
            let hello = try socket.readJSON()
            guard hello["version"] as? Int == 1, hello["kind"] as? String == "hello",
                  let value = hello["epoch"] as? String, !value.isEmpty else {
                throw RefinementFailure("invalid receiver epoch")
            }
            epoch = value
        } catch { finish(token: token, sequence: sequence, accepted: false, message: nil); return }
        guard unchanged(token, sequence: sequence) else { finish(token: token, sequence: sequence, accepted: false, message: nil); return }
        let config = SCStreamConfiguration()
        config.width = 1920; config.height = 1080
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.colorSpaceName = CGColorSpace.sRGB
        config.captureDynamicRange = .SDR
        config.showsCursor = true
        config.shouldBeOpaque = true
        let started = DispatchTime.now().uptimeNanoseconds
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { [weak self] image, error in
            guard let self else { return }
            self.queue.async {
                do {
                    guard self.unchanged(token, sequence: sequence) else { throw RefinementFailure("motion") }
                    guard let image, error == nil else { throw RefinementFailure("RGB capture failed") }
                    let raw = try RefinementPacket.bgra(image)
                    guard self.unchanged(token, sequence: sequence) else { throw RefinementFailure("motion") }
                    let payload = try RefinementPacket.compress(raw)
                    guard self.unchanged(token, sequence: sequence) else { throw RefinementFailure("motion") }
                    let header: [String: Any] = ["version": 1, "kind": "snapshot", "epoch": epoch,
                        "sequence": sequence, "width": 1920, "height": 1080, "stride": 7680,
                        "encoding": "bgra-zlib", "payload_bytes": payload.count]
                    try socket.writeJSON(header)
                    try socket.write(payload, rateLimitBytesPerSecond: 2_000_000)
                    let response = try socket.readJSON()
                    let accepted = response["version"] as? Int == 1 && response["kind"] as? String == "result"
                        && (response["sequence"] as? NSNumber)?.uint64Value == sequence
                        && response["accepted"] as? Bool == true
                    let ms = Double(DispatchTime.now().uptimeNanoseconds - started) / 1e6
                    self.finish(token: token, sequence: sequence, accepted: accepted, message:
                        "refinement sequence=\(sequence) accepted=\(accepted) raw_bytes=\(raw.count) compressed_bytes=\(payload.count) elapsed_ms=\(Int(ms)) reason=\(response["reason"] as? String ?? "unknown")")
                } catch {
                    self.finish(token: token, sequence: sequence, accepted: false, message: self.unchanged(token, sequence: sequence)
                        ? "refinement deferred reason=\(error)" : nil)
                }
            }
        }
    }

    private func finish(token: UInt64, sequence: UInt64, accepted: Bool, message: String?) {
        lock.withLock {
            working = false
            if accepted && generation == token && lastSequence == sequence {
                completedIdentity = (token, sequence)
            }
            if !accepted { retryAfterNS = DispatchTime.now().uptimeNanoseconds + 750_000_000 }
        }
        if let message { report(message) }
    }
}

/// Pure byte transformations are separately exercised without capturing user content.
enum RefinementPacket {
    static func bgra(_ image: CGImage) throws -> Data {
        guard image.width == 1920, image.height == 1080, let space = CGColorSpace(name: CGColorSpace.sRGB) else {
            throw RefinementFailure("unexpected RGB snapshot dimensions")
        }
        var result = Data(count: 1920 * 1080 * 4)
        try result.withUnsafeMutableBytes { raw in
            guard let ctx = CGContext(data: raw.baseAddress, width: 1920, height: 1080,
                bitsPerComponent: 8, bytesPerRow: 7680, space: space,
                bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.premultipliedFirst.rawValue) else {
                throw RefinementFailure("RGB conversion failed")
            }
            ctx.setBlendMode(.copy)
            ctx.interpolationQuality = .none
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: 1920, height: 1080))
            // Reject unexpected alpha instead of darkening premultiplied colors by forcing opacity.
            let bytes = raw.bindMemory(to: UInt8.self)
            for index in stride(from: 3, to: bytes.count, by: 4) {
                guard bytes[index] == 255 else { throw RefinementFailure("RGB snapshot unexpectedly transparent") }
            }
        }
        return result
    }

    static func compress(_ raw: Data) throws -> Data {
        var length = compressBound(uLong(raw.count))
        var output = Data(count: Int(length))
        let status = output.withUnsafeMutableBytes { destination in
            raw.withUnsafeBytes { source in
                compress2(destination.bindMemory(to: UInt8.self).baseAddress!, &length,
                          source.bindMemory(to: UInt8.self).baseAddress!, uLong(raw.count), 1)
            }
        }
        guard status == Z_OK, length <= 10 * 1024 * 1024 else { throw RefinementFailure("RGB compression failed") }
        output.count = Int(length)
        return output
    }
}

final class RefinementSocket {
    private var fd: Int32 = -1
    private let valid: () -> Bool
    init(port: UInt16, valid: @escaping () -> Bool) throws {
        self.valid = valid
        fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw RefinementFailure("refinement socket") }
        var yes: Int32 = 1, buffer: Int32 = 32_768
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer, socklen_t(MemoryLayout.size(ofValue: buffer)))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); address.sin_family = sa_family_t(AF_INET)
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1")); address.sin_port = port.bigEndian
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard result == 0 else { Darwin.close(fd); fd = -1; throw RefinementFailure("refinement unavailable") }
        let flags = fcntl(fd, F_GETFL)
        guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 else {
            Darwin.close(fd); fd = -1; throw RefinementFailure("refinement nonblocking")
        }
    }
    deinit { if fd >= 0 { Darwin.close(fd) } }

    private func wait(_ events: Int16, until deadline: UInt64) throws {
        guard valid() else { throw RefinementFailure("motion") }
        guard DispatchTime.now().uptimeNanoseconds < deadline else { throw RefinementFailure("refinement deadline") }
        var descriptor = pollfd(fd: fd, events: events, revents: 0)
        let result = poll(&descriptor, 1, 20)
        if result < 0 && errno != EINTR { throw RefinementFailure("refinement poll") }
        if descriptor.revents & Int16(POLLERR | POLLHUP | POLLNVAL) != 0 { throw RefinementFailure("refinement disconnected") }
    }
    private func read(_ count: Int) throws -> Data {
        var data = Data(count: count)
        let deadline = DispatchTime.now().uptimeNanoseconds + 3_000_000_000
        try data.withUnsafeMutableBytes { bytes in
            var offset = 0
            while offset < count {
                guard valid() else { throw RefinementFailure("motion") }
                guard DispatchTime.now().uptimeNanoseconds < deadline else { throw RefinementFailure("refinement deadline") }
                let n = recv(fd, bytes.baseAddress!.advanced(by: offset), count-offset, 0)
                if n > 0 { offset += n; continue }
                if n < 0 && errno == EINTR { continue }
                guard n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) else { throw RefinementFailure("refinement EOF") }
                try wait(Int16(POLLIN), until: deadline)
            }
        }
        return data
    }
    func readJSON() throws -> [String: Any] {
        let size = try read(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard (1...4096).contains(size),
              let result = try JSONSerialization.jsonObject(with: read(Int(size))) as? [String: Any] else {
            throw RefinementFailure("invalid refinement message")
        }
        return result
    }
    func writeJSON(_ object: [String: Any]) throws {
        let json = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        guard json.count <= 4096 else { throw RefinementFailure("refinement header too large") }
        var length = UInt32(json.count).bigEndian
        var packet = withUnsafeBytes(of: &length) { Data($0) }; packet.append(json)
        try write(packet)
    }
    func write(_ data: Data, rateLimitBytesPerSecond: Int = 0) throws {
        let start = DispatchTime.now().uptimeNanoseconds
        let deadline = start + 8_000_000_000
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < data.count {
                guard valid() else { throw RefinementFailure("motion") }
                guard DispatchTime.now().uptimeNanoseconds < deadline else { throw RefinementFailure("refinement deadline") }
                if rateLimitBytesPerSecond > 0 {
                    let target = start + UInt64(offset) * 1_000_000_000 / UInt64(rateLimitBytesPerSecond)
                    while DispatchTime.now().uptimeNanoseconds < target {
                        guard valid() else { throw RefinementFailure("motion") }
                        usleep(1000)
                    }
                }
                let n = send(fd, bytes.baseAddress!.advanced(by: offset), min(16_384, data.count-offset), 0)
                if n > 0 { offset += n; continue }
                if n < 0 && errno == EINTR { continue }
                guard n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) else { throw RefinementFailure("refinement send") }
                try wait(Int16(POLLOUT), until: deadline)
            }
        }
    }
}
