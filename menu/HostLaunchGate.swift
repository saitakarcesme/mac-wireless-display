import Foundation

/// Main-thread state for a cancellable delay followed by an uncancellable OS launch.
/// In-flight tokens survive disconnect so a timed-out launch blocks reconnect.
final class HostLaunchGate {
    private(set) var scheduled: Int?
    private(set) var inFlight = Set<Int>()
    var isBusy: Bool { scheduled != nil || !inFlight.isEmpty }

    func schedule(_ token: Int) -> Bool {
        guard !isBusy else { return false }
        scheduled = token
        return true
    }

    func begin(_ token: Int) -> Bool {
        guard scheduled == token else { return false }
        scheduled = nil
        inFlight.insert(token)
        return true
    }

    func cancelScheduled(_ token: Int) {
        if scheduled == token { scheduled = nil }
    }

    func complete(_ token: Int) { inFlight.remove(token) }
}
