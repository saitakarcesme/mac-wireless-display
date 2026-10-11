// Access only from the serial capture queue. A pending frame is raw owned pixels,
// never an encoded access unit: replacement cannot break H.264 references.
struct LatestFrameGate<Frame> {
    struct Pending {
        let frame: Frame
        let offeredAtNS: UInt64
        let wasDeferred: Bool
    }

    private(set) var inFlight = false
    private var pending: Pending?
    var hasPending: Bool { pending != nil }

    @discardableResult
    mutating func offer(_ frame: Frame, at now: UInt64, blockedUntil: UInt64 = 0) -> Bool {
        let replaced = pending != nil
        pending = Pending(frame: frame, offeredAtNS: now,
                          wasDeferred: inFlight || now < blockedUntil)
        return replaced
    }

    mutating func takeIfReady(at now: UInt64, blockedUntil: UInt64 = 0) -> Pending? {
        guard !inFlight, now >= blockedUntil, let result = pending else { return nil }
        pending = nil
        inFlight = true
        return result
    }

    mutating func complete() {
        precondition(inFlight, "An encode/write must be active before completion")
        inFlight = false
    }
}
