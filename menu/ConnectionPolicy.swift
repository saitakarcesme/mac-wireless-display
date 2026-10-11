import Foundation

/// Monotonic retry state. An idle desktop is not a failed connection.
struct ConnectionPolicy {
    private(set) var requested = false
    private(set) var attempts = 0
    private(set) var retryAt: TimeInterval?

    mutating func request() { requested = true; retryAt = nil }
    mutating func cancel() { requested = false; attempts = 0; retryAt = nil }
    mutating func healthy() { attempts = 0; retryAt = nil }
    mutating func failed(now: TimeInterval) {
        guard requested else { return }
        attempts = min(attempts + 1, 30)
        retryAt = now + min(30, pow(2, Double(min(attempts, 5))))
    }
    func ready(now: TimeInterval) -> Bool { requested && retryAt.map { now >= $0 } == true }
}
