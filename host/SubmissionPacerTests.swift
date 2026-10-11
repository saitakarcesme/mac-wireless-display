import Foundation

/// Deterministic integration harness: the production gate, pacer and wake token
/// state run against a fake monotonic clock and an adversarial timer queue.
private final class Simulation {
    enum Kind { case completion(Int), timer(UInt64) }
    struct Event { let at: UInt64; let order: Int; let kind: Kind }
    var now: UInt64 = 0
    var retryAfter: UInt64 = 0
    var stopped = false
    var gate = LatestFrameGate<Int>()
    var pacer: SubmissionPacer
    var wake = DrainWakeup()
    var sent: [(frame: Int, at: UInt64)] = []
    var events: [Event] = []
    var order = 0, staleTimers = 0, scheduledTimers = 0
    var activeFrame: Int?
    let busyNS: UInt64

    init(enabled: Bool = true, busyNS: UInt64 = 8_000_000) {
        pacer = SubmissionPacer(enabled: enabled, fps: 60)
        self.busyNS = busyNS
    }

    func enqueue(_ kind: Kind, at: UInt64) {
        order += 1
        events.append(Event(at: at, order: order, kind: kind))
    }

    func cancelTimer() { wake.cancel() }

    func drain() {
        guard !stopped, gate.hasPending, !gate.inFlight else { cancelTimer(); return }
        let earliest = pacer.earliestSubmission(retryAfter: retryAfter)
        if now < earliest {
            if let token = wake.schedule(at: earliest) {
                scheduledTimers += 1
                enqueue(.timer(token), at: earliest)
            }
            return
        }
        cancelTimer()
        guard let pending = gate.takeIfReady(at: now, blockedUntil: earliest) else { return }
        precondition(activeFrame == nil)
        pacer.submitted(at: now)
        sent.append((pending.frame, now)); activeFrame = pending.frame
        enqueue(.completion(pending.frame), at: now + busyNS)
    }

    func advance(to deadline: UInt64, includeEqual: Bool = true, delayedCallbacks: Bool = false) {
        precondition(deadline >= now)
        while let index = events.indices.min(by: {
            events[$0].at == events[$1].at ? events[$0].order < events[$1].order : events[$0].at < events[$1].at
        }), events[index].at < deadline || (includeEqual && events[index].at == deadline) {
            let event = events.remove(at: index)
            now = delayedCallbacks ? deadline : event.at
            switch event.kind {
            case .completion(let id):
                if stopped { continue }
                precondition(activeFrame == id)
                activeFrame = nil; cancelTimer(); gate.complete(); drain()
            case .timer(let token):
                guard !stopped, wake.consume(token) else { staleTimers += 1; continue }
                drain()
            }
        }
        now = deadline
    }

    func capture(_ id: Int, at: UInt64, timerFirst: Bool = false) {
        advance(to: at, includeEqual: timerFirst)
        guard !stopped else { return }
        gate.offer(id, at: now, blockedUntil: pacer.earliestSubmission(retryAfter: retryAfter))
        drain()
        advance(to: at)
    }

    func stop() { stopped = true; cancelTimer() }

    func assertBounded() {
        for pair in zip(sent, sent.dropFirst()) {
            precondition(pair.1.at - pair.0.at >= pacer.intervalNS, "catch-up burst")
            precondition(pair.1.frame > pair.0.frame, "duplicate or stale submission")
        }
    }
}

@main
enum SubmissionPacerTests {
    static func main() {
        // Native120Hz input,8ms encode: no more than60 starts in the first second,
        // and the final input still drains without a subsequent capture callback.
        let regular = Simulation()
        for id in 0..<120 { regular.capture(id, at: (UInt64(id) * 1_000_000_000 + 119) / 120) }
        regular.advance(to: 1_050_000_000)
        precondition(regular.sent.filter { $0.at < 1_000_000_000 }.count <= 60)
        precondition(regular.sent.count >= 59 && regular.sent.last?.frame == 119)
        regular.assertBounded()

        // Late timers consume no accrued credit; the newest state wins after a
        // simulated250ms stall, then the next frame waits an entire new period.
        var pacer = SubmissionPacer(enabled: true, fps: 60)
        pacer.submitted(at: 0)
        pacer.submitted(at: 250_000_000)
        precondition(pacer.earliestSubmission(retryAfter: 0) == 266_666_667)
        let delayed = Simulation()
        delayed.capture(0, at: 0); delayed.capture(1, at: 9_000_000)
        delayed.advance(to: 250_000_000, delayedCallbacks: true)
        delayed.capture(2, at: 250_000_001)
        delayed.advance(to: 300_000_000)
        precondition(delayed.sent.map(\.at) == [0, 250_000_000, 266_666_667])
        delayed.assertBounded()
        let jitter = Simulation()
        let times: [UInt64] = [0, 1, 4_000_000, 16_000_000, 25_000_000, 60_000_000,
                               60_000_001, 61_000_000, 150_000_000, 150_000_001]
        for (id, time) in times.enumerated() { jitter.capture(id, at: time) }
        jitter.advance(to: 200_000_000)
        precondition(jitter.sent.last?.frame == times.count - 1)
        jitter.assertBounded()

        // A35ms encoder controls throughput without any extra queue. Completion
        // chooses the latest pending capture and preserves the final idle state.
        let busy = Simulation(busyNS: 35_000_000)
        for id in 0..<120 { busy.capture(id, at: UInt64(id) * 8_333_334) }
        busy.advance(to: 1_100_000_000)
        precondition(busy.sent.count <= 30 && busy.sent.last?.frame == 119)
        busy.assertBounded()

        // One new screen state while the pacer waits, then SCK becomes idle.
        let sparse = Simulation()
        sparse.capture(0, at: 0)
        sparse.capture(1, at: 9_000_000)
        sparse.advance(to: 30_000_000)
        precondition(sparse.sent.map(\.frame) == [0, 1])
        precondition(sparse.sent[1].at == 16_666_667)

        // Exercise both orderings when a capture races a due timer. Neither
        // ordering can duplicate an encode or lose the final new screen state.
        for timerFirst in [false, true] {
            let race = Simulation()
            race.capture(0, at: 0); race.capture(1, at: 9_000_000)
            race.capture(2, at: 16_666_667, timerFirst: timerFirst)
            race.advance(to: 60_000_000)
            precondition(race.sent.last?.frame == 2)
            precondition(race.sent.filter { $0.frame == 2 }.count == 1)
            race.assertBounded()
        }

        // A retry deadline replaces the rate timer. Deliver the old canceled
        // callback anyway; its token cannot consume the replacement's wakeup.
        let retry = Simulation()
        retry.capture(0, at: 0); retry.capture(1, at: 9_000_000)
        retry.retryAfter = 100_000_000; retry.cancelTimer(); retry.drain()
        retry.advance(to: 120_000_000)
        precondition(retry.staleTimers == 1 && retry.sent[1].at == 100_000_000)
        retry.assertBounded()

        let stopped = Simulation()
        stopped.capture(0, at: 0); stopped.capture(1, at: 9_000_000)
        stopped.stop(); stopped.advance(to: 1_000_000_000)
        precondition(stopped.sent.count == 1 && stopped.wake.deadlineNS == nil)

        // Opt-out preserves120Hz admission when the encoder can keep up.
        let uncapped = Simulation(enabled: false, busyNS: 1_000_000)
        for id in 0..<120 { uncapped.capture(id, at: UInt64(id) * 8_333_334) }
        uncapped.advance(to: 1_050_000_000)
        precondition(uncapped.sent.count == 120)
        uncapped.assertBounded()
        // Wake diagnostics cannot underflow on early callbacks and retain only
        // bounded samples while preserving lifetime totals for this interval.
        var wakes = DrainWakeMetrics()
        wakes.record(target: 100, fired: 99)
        wakes.record(target: 100, fired: 110)
        wakes.record(target: 100, fired: 100)
        precondition(wakes.count == 3 && wakes.earlyCount == 1)
        precondition(wakes.maximumLateNS == 10 && wakes.meanDeltaNS == 3)
        precondition(wakes.sampledMedianDeltaNS == 0)
        for index in 0..<1000 { wakes.record(target: 100, fired: 100 + UInt64(index)) }
        precondition(wakes.count == 1003 && wakes.samplesNS.count == 128)
        var extremes = DrainWakeMetrics()
        extremes.record(target: 0, fired: UInt64.max)
        extremes.record(target: UInt64.max, fired: 0)
        precondition(extremes.samplesNS[0].isFinite && extremes.samplesNS[0] > 0)
        precondition(extremes.samplesNS[1].isFinite && extremes.samplesNS[1] < 0)
        precondition(extremes.maximumLateNS == UInt64.max && extremes.earlyCount == 1)
        print("PASS:120Hz→60FPS; jitter/no catch-up; busy encoder; final idle frame; timer races/cancel/retry; stop; opt-out")
        print("PASS:wake metrics early/signed/median arithmetic and128-sample bound")
    }
}
