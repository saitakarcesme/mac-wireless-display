import Foundation
import Darwin

enum RecoveryTransportEvent {
    case active(epoch: UInt64)
    case inactive(reconnect: Bool)
    case request(RecoveryClaim)
}
private struct TransportFailure: Error, CustomStringConvertible {
    let description: String
    init(_ text: String) { description = text }
}

/// All descriptor, framing and lifecycle operations run on one serial queue.
/// An enabled read source owns closing its FD in its cancel handler, avoiding
/// descriptor-reuse races. No socket can leave loopback.
final class BoundedTransport {
    private var descriptor: Int32 = -1
    private(set) var connectionGeneration: UInt64 = 0
    private let port: UInt16
    private let queue: DispatchQueue
    private let enabled: Bool
    private let report: (String) -> Void
    var onRecovery: ((RecoveryTransportEvent) -> Void)?
    private var source: DispatchSourceRead?
    private var negotiationTimer: DispatchWorkItem?
    private var partialTimer: DispatchWorkItem?
    private var input = Data()
    private var partialDeadline = RecoveryDeadline()
    private var epoch: UInt64?
    private var negotiated = false
    private var suppressed = false
    private var handshakeDeadlineNS: UInt64 = 0
    private var latestSent: UInt64?
    private var lastRequest: UInt64 = 0
    private var rateWindowNS: UInt64 = 0
    private var rateCount = 0

    init(port: UInt16, queue: DispatchQueue, recoveryEnabled: Bool, report: @escaping (String) -> Void) {
        self.port = port; self.queue = queue; self.enabled = recoveryEnabled; self.report = report
    }
    deinit {
        negotiationTimer?.cancel(); partialTimer?.cancel()
        if let source { source.cancel() }
        else if descriptor >= 0 { Darwin.close(descriptor) }
    }
    func disconnect() {
        dispatchPrecondition(condition: .onQueue(queue))
        negotiationTimer?.cancel(); negotiationTimer = nil
        partialTimer?.cancel(); partialTimer = nil
        if descriptor >= 0 {
            Darwin.shutdown(descriptor, SHUT_RDWR)
            if let source { source.cancel() } else { Darwin.close(descriptor) }
        }
        source = nil; descriptor = -1; input.removeAll(keepingCapacity: false)
        partialDeadline.clear(); epoch = nil; negotiated = false; latestSent = nil
        if enabled { onRecovery?(.inactive(reconnect: true)) }
    }
    private func failControl(_ reason: String) {
        if !negotiated { suppressed = true }
        report("recovery control reset reason=\(reason) legacy_fallback=\(suppressed)")
        disconnect()
    }
    private func connectIfNeeded() throws {
        dispatchPrecondition(condition: .onQueue(queue))
        if descriptor >= 0 { return }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw TransportFailure("socket errno=\(errno)") }
        descriptor = fd
        var yes: Int32 = 1, buffer: Int32 = 65_536
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer, socklen_t(MemoryLayout.size(ofValue: buffer)))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET); address.sin_port = port.bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard connected == 0 else { let error = errno; disconnect(); throw TransportFailure("connect errno=\(error)") }
        let flags = fcntl(fd, F_GETFL)
        guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 else {
            disconnect(); throw TransportFailure("nonblocking socket configuration failed")
        }
        connectionGeneration &+= 1
        report("transport connected target=127.0.0.1:\(port)")
        if enabled && !suppressed {
            let currentEpoch = UInt64.random(in: 1...UInt64.max)
            epoch = currentEpoch; negotiated = false; latestSent = nil; lastRequest = 0
            rateWindowNS = DispatchTime.now().uptimeNanoseconds; rateCount = 0
            handshakeDeadlineNS = rateWindowNS + 1_000_000_000
            let generation = connectionGeneration
            let reader = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
            reader.setEventHandler { [weak self] in
                guard let self, self.connectionGeneration == generation, self.descriptor == fd else { return }
                self.readControl()
            }
            reader.setCancelHandler { Darwin.close(fd) }
            source = reader; reader.resume()
            let timeout = DispatchWorkItem { [weak self] in
                guard let self, self.epoch == currentEpoch, !self.negotiated, !self.suppressed else { return }
                self.failControl("negotiation timeout")
            }
            negotiationTimer = timeout
            queue.asyncAfter(deadline: DispatchTime(uptimeNanoseconds: handshakeDeadlineNS), execute: timeout)
            do { _ = try writeRaw(RecoveryRecord(opcode: .hello, epoch: currentEpoch, requestID: 0, sequence: .max).encoded()) }
            catch { suppressed = true; disconnect(); throw error }
        }
    }
    private func readControl() {
        dispatchPrecondition(condition: .onQueue(queue))
        do {
            while descriptor >= 0 {
                var bytes = [UInt8](repeating: 0, count: RecoveryRecord.length - input.count)
                let count = Darwin.recv(descriptor, &bytes, bytes.count, 0)
                if count < 0 && errno == EINTR { continue }
                if count < 0 && (errno == EWOULDBLOCK || errno == EAGAIN) { return }
                guard count > 0 else { throw TransportFailure("control EOF or recv errno=\(errno)") }
                let now = DispatchTime.now().uptimeNanoseconds
                if partialDeadline.expired(at: now) {
                    throw TransportFailure("partial control deadline")
                }
                input.append(contentsOf: bytes.prefix(count))
                if input.count < RecoveryRecord.length {
                    if partialDeadline.deadlineNS == nil {
                        let token = partialDeadline.begin(at: now, duration: 250_000_000)
                        let generation = connectionGeneration
                        let timeout = DispatchWorkItem { [weak self] in
                            guard let self, self.connectionGeneration == generation,
                                  self.partialDeadline.expired(token: token, at: DispatchTime.now().uptimeNanoseconds),
                                  !self.input.isEmpty else { return }
                            self.failControl("partial control timeout")
                        }
                        partialTimer = timeout
                        queue.asyncAfter(deadline: .now() + .milliseconds(250), execute: timeout)
                    }
                    continue
                }
                partialTimer?.cancel(); partialTimer = nil; partialDeadline.clear()
                let record = try RecoveryRecord.decode(input); input.removeAll(keepingCapacity: true)
                if now - rateWindowNS >= 1_000_000_000 { rateWindowNS = now; rateCount = 0 }
                rateCount += 1
                guard rateCount <= 16 else { throw TransportFailure("control rate exceeded") }
                try accept(record, at: now)
            }
        } catch { failControl(String(describing: error)) }
    }
    private func accept(_ record: RecoveryRecord, at now: UInt64) throws {
        guard record.epoch == epoch else { throw TransportFailure("wrong control epoch") }
        switch record.opcode {
        case .accept:
            guard !negotiated, !suppressed, now <= handshakeDeadlineNS else { throw TransportFailure("unexpected or late ACCEPT") }
            negotiated = true; negotiationTimer?.cancel(); negotiationTimer = nil
            onRecovery?(.active(epoch: record.epoch))
            report("recovery control negotiated")
        case .decline:
            guard !negotiated, !suppressed else { throw TransportFailure("unexpected DECLINE") }
            // Stay connected with ordinary video; keep reading to bound any peer misuse.
            suppressed = true; negotiationTimer?.cancel(); negotiationTimer = nil
            onRecovery?(.inactive(reconnect: false))
            report("recovery control declined; effective GOP remains30")
        case .requestIDR:
            guard negotiated, record.requestID > lastRequest else { throw TransportFailure("unnegotiated or replayed request") }
            guard record.sequence == .max || (latestSent != nil && record.sequence <= latestSent!) else {
                throw TransportFailure("future recovery sequence")
            }
            lastRequest = record.requestID
            onRecovery?(.request(RecoveryClaim(epoch: record.epoch, requestID: record.requestID)))
        default: throw TransportFailure("unexpected control direction")
        }
    }
    func write(_ data: Data, sequence: UInt64, keyframe: Bool, recovery: RecoveryClaim?) throws -> (durationNS: UInt64, blocked: Bool) {
        try connectIfNeeded()
        do {
            let result = try writeRaw(data)
            latestSent = sequence
            if keyframe, let recovery, negotiated, recovery.epoch == epoch {
                _ = try writeRaw(RecoveryRecord(opcode: .idrSent, epoch: recovery.epoch, requestID: recovery.requestID, sequence: sequence).encoded())
                report("recovery IDR sent request=\(recovery.requestID) sequence=\(sequence)")
            }
            return result
        } catch {
            if epoch != nil && !negotiated { suppressed = true }
            disconnect(); throw error
        }
    }
    private func writeRaw(_ data: Data) throws -> (durationNS: UInt64, blocked: Bool) {
        let start = DispatchTime.now().uptimeNanoseconds, deadline = start + 250_000_000
        var blocked = false
        try data.withUnsafeBytes { buffer in
            guard let base = buffer.baseAddress, descriptor >= 0 else { throw TransportFailure("empty packet or closed socket") }
            var offset = 0
            while offset < buffer.count {
                let now = DispatchTime.now().uptimeNanoseconds
                guard now < deadline else { throw TransportFailure("write deadline250ms") }
                let count = Darwin.send(descriptor, base.advanced(by: offset), buffer.count - offset, 0)
                if count > 0 { offset += count; continue }
                if count < 0 && errno == EINTR { continue }
                guard count < 0 && (errno == EWOULDBLOCK || errno == EAGAIN) else { throw TransportFailure("send errno=\(errno)") }
                blocked = true
                var pollFD = pollfd(fd: descriptor, events: Int16(POLLOUT), revents: 0)
                let remaining = Int32(max(1, min(250, (deadline - now) / 1_000_000)))
                let result = poll(&pollFD, 1, remaining)
                if result < 0 && errno == EINTR { continue }
                guard result > 0, (pollFD.revents & Int16(POLLERR | POLLHUP | POLLNVAL)) == 0 else {
                    throw TransportFailure("write poll timeout or disconnect")
                }
            }
        }
        return (DispatchTime.now().uptimeNanoseconds - start, blocked)
    }
}
