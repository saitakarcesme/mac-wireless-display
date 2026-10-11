import Foundation

@main
enum LatestFrameGateTests {
    static func main() {
        var gate = LatestFrameGate<String>()
        precondition(!gate.offer("moving-window", at: 0))
        precondition(gate.takeIfReady(at: 0)?.frame == "moving-window")
        precondition(gate.inFlight && !gate.hasPending)

        // The final desktop frame arrives while the previous AU is encoding.
        // No more capture callbacks will arrive after the display becomes idle.
        precondition(!gate.offer("window-closing", at: 5_000_000))
        precondition(gate.offer("closed-desktop", at: 8_000_000))
        precondition(gate.takeIfReady(at: 8_000_000) == nil)
        gate.complete()
        let final = gate.takeIfReady(at: 10_000_000)
        precondition(final?.frame == "closed-desktop")
        precondition(final?.wasDeferred == true)
        precondition(final?.offeredAtNS == 8_000_000)
        precondition(!gate.hasPending && gate.inFlight)
        gate.complete()
        precondition(gate.takeIfReady(at: 20_000_000) == nil)

        // Network retry waits retain only the newest raw state. The retry timer
        // may drain it without receiving a further screen-change callback.
        precondition(!gate.offer("old-after-failure", at: 100, blockedUntil: 500))
        precondition(gate.offer("latest-after-failure", at: 200, blockedUntil: 500))
        precondition(gate.takeIfReady(at: 499, blockedUntil: 500) == nil)
        precondition(gate.hasPending && !gate.inFlight)
        precondition(gate.takeIfReady(at: 500, blockedUntil: 500)?.frame == "latest-after-failure")
        gate.complete()

        // Arbitrarily many input changes still retain one pending raw frame.
        gate.offer("active", at: 0)
        precondition(gate.takeIfReady(at: 0)?.frame == "active")
        for index in 0..<10_000 { gate.offer(String(index), at: UInt64(index)) }
        gate.complete()
        precondition(gate.takeIfReady(at: 10_000)?.frame == "9999")
        precondition(!gate.hasPending)
        gate.complete()
        print("PASS: final idle state drains; only newest raw frame retained; backoff preserves final state")
    }
}
