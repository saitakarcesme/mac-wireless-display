/// Serial-queue-owned monotonic encoder admission policy. It never accumulates
/// credits: every submission starts a new interval from its actual start time.
struct SubmissionPacer {
    let intervalNS: UInt64
    private(set) var nextSubmissionNS: UInt64 = 0

    init(enabled: Bool, fps: Int32) {
        precondition((1...60).contains(fps))
        let rate = UInt64(fps)
        intervalNS = enabled ? (1_000_000_000 + rate - 1) / rate : 0
    }

    func earliestSubmission(retryAfter: UInt64) -> UInt64 {
        max(nextSubmissionNS, retryAfter)
    }

    mutating func submitted(at now: UInt64) {
        guard intervalNS != 0 else { return }
        let addition = now.addingReportingOverflow(intervalNS)
        nextSubmissionNS = addition.overflow ? UInt64.max : addition.partialValue
    }
}

/// A canceled DispatchWorkItem may already be enqueued. Tokens prevent that
/// stale callback from draining a frame or clearing a replacement timer.
struct DrainWakeup {
    private var generation: UInt64 = 0
    private(set) var deadlineNS: UInt64?

    mutating func schedule(at deadline: UInt64) -> UInt64? {
        guard deadlineNS != deadline else { return nil }
        generation &+= 1
        deadlineNS = deadline
        return generation
    }

    mutating func cancel() {
        generation &+= 1
        deadlineNS = nil
    }

    mutating func consume(_ token: UInt64) -> Bool {
        guard deadlineNS != nil, generation == token else { return false }
        deadlineNS = nil
        return true
    }
}

/// Bounded diagnostic samples. Signed delta also detects an unexpectedly early
/// callback; admission rechecks its deadline regardless of this measurement.
struct DrainWakeMetrics {
    private(set) var count = 0
    private(set) var earlyCount = 0
    private(set) var deltaSumNS = 0.0
    private(set) var maximumLateNS: UInt64 = 0
    private(set) var samplesNS: [Double] = []

    mutating func record(target: UInt64, fired: UInt64) {
        let delta: Double
        if fired >= target {
            let late = fired - target
            maximumLateNS = max(maximumLateNS, late)
            delta = Double(late)
        } else {
            earlyCount += 1
            delta = -Double(target - fired)
        }
        count += 1; deltaSumNS += delta
        if samplesNS.count < 128 { samplesNS.append(delta) }
        else { samplesNS[(count - 1) % 128] = delta }
    }

    var meanDeltaNS: Double { count == 0 ? 0 : deltaSumNS / Double(count) }
    var sampledMedianDeltaNS: Double {
        let ordered = samplesNS.sorted()
        guard !ordered.isEmpty else { return 0 }
        let middle = ordered.count / 2
        return ordered.count.isMultiple(of: 2)
            ? (ordered[middle - 1] + ordered[middle]) / 2 : ordered[middle]
    }
}
