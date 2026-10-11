import Foundation

@main
private enum LaunchGateTests {
    static func main() {
        // Disconnect before the 1-second delay prevents any late OS launch.
        let deferred = HostLaunchGate()
        precondition(deferred.schedule(1))
        deferred.cancelScheduled(1)
        precondition(!deferred.begin(1))
        precondition(!deferred.isBusy)
        precondition(deferred.schedule(2))
        precondition(deferred.begin(2))
        deferred.complete(2)

        // Disconnect during NSWorkspace cannot enable another launch, even after
        // its bounded close wait returns the menu to idle. Only completion frees it.
        let opening = HostLaunchGate()
        precondition(opening.schedule(7))
        precondition(opening.begin(7))
        opening.cancelScheduled(7)
        precondition(opening.isBusy)
        precondition(!opening.schedule(8))
        opening.complete(7)
        precondition(!opening.isBusy)
        precondition(opening.schedule(8))
        precondition(opening.begin(8))
        // A duplicate or stale callback cannot clear the newer launch token.
        opening.complete(7)
        precondition(opening.inFlight == [8])
        opening.complete(8)

        let formatter = ISO8601DateFormatter()
        let date = formatter.date(from: "2026-10-11T02:20:00+0200")
        precondition(date != nil)
        precondition(date == formatter.date(from: "2026-10-11T00:20:00Z"))
        if let actualTimestamp = CommandLine.arguments.dropFirst().first {
            precondition(formatter.date(from: actualTimestamp) != nil,
                         "Actual receiver timestamp failed to parse: \(actualTimestamp)")
        }
        print("Launch cancellation, stale callback isolation, reconnect gate, and receiver timestamp checks passed")
    }
}
