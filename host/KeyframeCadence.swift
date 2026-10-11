import Foundation
import CoreFoundation

/// Counts submitted video frames, not capture callbacks or elapsed idle time.
/// RTVC low-latency mode otherwise uses an infinite GOP. Recovery still takes
/// precedence over this periodic fallback; no setting changes the wire format.
struct KeyframeCadence {
    static let defaultInterval = 30
    static let allowedIntervals = 30...600

    enum InvalidInterval: Error { case expectedInteger30Through600 }

    static func validatedInterval(_ value: Any) throws -> Int {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite,
              allowedIntervals.contains(number.intValue),
              number.doubleValue == Double(number.intValue) else {
            throw InvalidInterval.expectedInteger30Through600
        }
        return number.intValue
    }

    let interval: Int
    private var submittedSinceRequest: Int

    init(interval: Int = defaultInterval) {
        precondition(Self.allowedIntervals.contains(interval))
        self.interval = interval
        submittedSinceRequest = interval // Always make the first submission IDR.
    }

    mutating func nextSubmission(forceRecovery: Bool) -> Bool {
        let force = forceRecovery || submittedSinceRequest >= interval
        submittedSinceRequest = force ? 1 : submittedSinceRequest + 1
        return force
    }
}
