import Foundation

/// Fixed-size records carried only inside the existing authenticated video TCP
/// connection. The random epoch is a replay boundary, not authentication.
struct RecoveryRecord: Equatable {
    enum Opcode: UInt8 { case hello = 1, accept = 2, requestIDR = 3, idrSent = 4, decline = 5 }
    enum Invalid: Error { case framing, fields }
    static let length = 32
    static let noSequence = UInt64.max
    let opcode: Opcode
    let epoch: UInt64
    let requestID: UInt64
    let sequence: UInt64

    func encoded() -> Data {
        var result = Data([0x48, 0x43, 0x54, 0x4c, 1, opcode.rawValue, 0, 0])
        for value in [epoch, requestID, sequence] {
            var big = value.bigEndian
            withUnsafeBytes(of: &big) { result.append(contentsOf: $0) }
        }
        return result
    }

    static func decode(_ data: Data) throws -> RecoveryRecord {
        guard data.count == length else { throw Invalid.framing }
        let bytes = Array(data)
        guard bytes[0..<4].elementsEqual([0x48, 0x43, 0x54, 0x4c]), bytes[4] == 1,
              let opcode = Opcode(rawValue: bytes[5]), bytes[6] == 0, bytes[7] == 0 else {
            throw Invalid.framing
        }
        func integer(_ start: Int) -> UInt64 {
            bytes[start..<(start + 8)].reduce(0) { ($0 << 8) | UInt64($1) }
        }
        let result = RecoveryRecord(opcode: opcode, epoch: integer(8), requestID: integer(16), sequence: integer(24))
        guard result.epoch != 0 else { throw Invalid.fields }
        switch opcode {
        case .hello, .accept, .decline:
            guard result.requestID == 0, result.sequence == noSequence else { throw Invalid.fields }
        case .requestIDR:
            guard result.requestID > 0 else { throw Invalid.fields }
        case .idrSent:
            guard result.requestID > 0, result.sequence != noSequence else { throw Invalid.fields }
        }
        return result
    }
}

struct RecoveryClaim: Equatable {
    let epoch: UInt64
    let requestID: UInt64
}

/// A canceled callback may already be queued. Identity, not a reused timestamp,
/// prevents it from expiring a later partial record on the same TCP connection.
struct RecoveryDeadline {
    private var generation: UInt64 = 0
    private(set) var deadlineNS: UInt64?
    mutating func begin(at now: UInt64, duration: UInt64) -> UInt64 {
        generation &+= 1
        deadlineNS = now > UInt64.max - duration ? UInt64.max : now + duration
        return generation
    }
    mutating func clear() { generation &+= 1; deadlineNS = nil }
    func expired(token: UInt64, at now: UInt64) -> Bool {
        generation == token && expired(at: now)
    }
    func expired(at now: UInt64) -> Bool { deadlineNS.map { now >= $0 } ?? false }
}

/// Capture-queue-owned policy; it contains no clocks, timers, sockets or pixels.
/// Live input never waits for recovery. A request only forces the next eligible
/// submission, or obtains one bounded idle screenshot when no raw frame exists.
struct RecoveryRequestGate {
    struct Snapshot: Equatable {
        let serial: UInt64
        let epoch: UInt64?
        let captureGeneration: UInt64
        let deadlineNS: UInt64
    }
    static let minimumIDRIntervalNS: UInt64 = 250_000_000
    private(set) var epoch: UInt64?
    private(set) var pendingID: UInt64?
    private(set) var nextAllowedNS: UInt64 = 0
    private(set) var captureGeneration: UInt64 = 0
    private(set) var snapshot: Snapshot?
    private var serial: UInt64 = 0

    mutating func activate(epoch: UInt64?) {
        self.epoch = epoch; pendingID = nil; nextAllowedNS = 0
        // Do not clear a physically outstanding screenshot. Its eventual result
        // must be rejected by epoch/generation, not allow a second concurrent API call.
        captureGeneration &+= 1
    }
    mutating func request(_ claim: RecoveryClaim) -> Bool {
        guard claim.epoch == epoch else { return false }
        pendingID = max(pendingID ?? 0, claim.requestID)
        return true
    }
    mutating func capturedLiveFrame() { captureGeneration &+= 1 }
    mutating func claim(at now: UInt64) -> RecoveryClaim? {
        guard let epoch, let id = pendingID, now >= nextAllowedNS else { return nil }
        pendingID = nil
        nextAllowedNS = now > UInt64.max - Self.minimumIDRIntervalNS ? UInt64.max : now + Self.minimumIDRIntervalNS
        return RecoveryClaim(epoch: epoch, requestID: id)
    }
    mutating func beginSnapshot(at now: UInt64, allowWithoutRequest: Bool = false) -> Snapshot? {
        guard snapshot == nil, (epoch != nil && pendingID != nil || allowWithoutRequest), now >= nextAllowedNS else { return nil }
        serial &+= 1
        let deadline = now > UInt64.max - 1_000_000_000 ? UInt64.max : now + 1_000_000_000
        let token = Snapshot(serial: serial, epoch: epoch, captureGeneration: captureGeneration, deadlineNS: deadline)
        snapshot = token
        return token
    }
    mutating func finishSnapshot(_ token: Snapshot, at now: UInt64, allowWithoutRequest: Bool = false) -> Bool {
        guard snapshot == token else { return false }
        snapshot = nil
        return epoch == token.epoch && captureGeneration == token.captureGeneration
            && now <= token.deadlineNS && (pendingID != nil || allowWithoutRequest)
    }
    // Timeout invalidates acceptance but leaves the physical single-flight slot
    // occupied until the framework callback returns; no unbounded API calls.
    mutating func timeoutSnapshot(_ token: Snapshot) {
        guard snapshot == token else { return }
        captureGeneration &+= 1
    }
}
