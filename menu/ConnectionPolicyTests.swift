import Foundation
@main enum Tests {
    static func main() {
        var p = ConnectionPolicy()
        p.failed(now: 0); precondition(!p.ready(now: 100))
        p.request(); p.failed(now: 0)
        precondition(!p.ready(now: 1) && p.ready(now: 2))
        // A long outage never exhausts the retry budget.
        for i in 0..<10000 {
            let now = Double(i * 31)
            p.failed(now: now)
            precondition(p.retryAt! - now <= 30 && p.ready(now: now + 30))
        }
        p.healthy(); precondition(p.requested && p.attempts == 0 && p.retryAt == nil)
        p.failed(now: 1); p.cancel(); precondition(!p.ready(now: 100000))
        p.request(); precondition(p.requested && p.retryAt == nil)
        print("Connection policy: retry, cancellation, recovery and prolonged outage passed")
    }
}
