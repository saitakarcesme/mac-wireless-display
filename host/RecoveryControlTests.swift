import Foundation

@main enum RecoveryControlTests {
    static func main() throws {
        for opcode in [RecoveryRecord.Opcode.hello, .accept, .decline, .requestIDR, .idrSent] {
            let command = opcode == .requestIDR || opcode == .idrSent
            let record = RecoveryRecord(opcode: opcode, epoch: 42, requestID: command ? 7 : 0,
                                        sequence: opcode == .idrSent ? 12 : .max)
            precondition(record.encoded().count == 32)
            let decoded = try RecoveryRecord.decode(record.encoded())
            precondition(decoded == record)
        }
        let hello = RecoveryRecord(opcode: .hello, epoch: 42, requestID: 0, sequence: .max).encoded()
        func rejects(_ data: Data) {
            do { _ = try RecoveryRecord.decode(data); preconditionFailure("invalid record accepted") }
            catch {}
        }
        rejects(hello.prefix(31)); rejects(hello + Data([0]))
        for index in [0, 4, 5, 6, 7] {
            var changed = hello; changed[index] = 255; rejects(changed)
        }
        rejects(RecoveryRecord(opcode: .hello, epoch: 0, requestID: 0, sequence: .max).encoded())
        rejects(RecoveryRecord(opcode: .hello, epoch: 42, requestID: 1, sequence: .max).encoded())
        rejects(RecoveryRecord(opcode: .requestIDR, epoch: 42, requestID: 0, sequence: .max).encoded())
        rejects(RecoveryRecord(opcode: .idrSent, epoch: 42, requestID: 1, sequence: .max).encoded())

        var deadline = RecoveryDeadline()
        let oldDeadline = deadline.begin(at: 0, duration: 250)
        precondition(!deadline.expired(token: oldDeadline, at: 249))
        deadline.clear()
        let newDeadline = deadline.begin(at: 0, duration: 500) // Even identical clock values are safe.
        precondition(!deadline.expired(token: oldDeadline, at: 600))
        precondition(!deadline.expired(token: newDeadline, at: 499))
        precondition(deadline.expired(token: newDeadline, at: 500))
        deadline.clear(); precondition(!deadline.expired(token: newDeadline, at: 900))

        var gate = RecoveryRequestGate()
        precondition(!gate.request(RecoveryClaim(epoch: 42, requestID: 1)))
        gate.activate(epoch: 42)
        precondition(!gate.request(RecoveryClaim(epoch: 41, requestID: 1)))
        precondition(gate.request(RecoveryClaim(epoch: 42, requestID: 1)))
        precondition(gate.claim(at: 100) == RecoveryClaim(epoch: 42, requestID: 1))
        precondition(gate.claim(at: 101) == nil)
        for id in 2...100 { precondition(gate.request(RecoveryClaim(epoch: 42, requestID: UInt64(id)))) }
        precondition(gate.pendingID == 100)
        precondition(gate.claim(at: 250_000_099) == nil)
        precondition(gate.claim(at: 250_000_100)?.requestID == 100)
        precondition(gate.pendingID == nil)

        // An idle request creates exactly one snapshot. Its final state can be
        // submitted without waiting for another live capture callback.
        gate.activate(epoch: 43)
        precondition(gate.request(RecoveryClaim(epoch: 43, requestID: 1)))
        let idle = gate.beginSnapshot(at: 1_000)!
        precondition(gate.beginSnapshot(at: 2_000) == nil)
        precondition(gate.finishSnapshot(idle, at: 30_000))
        precondition(gate.claim(at: 30_000)?.requestID == 1)

        // Newer motion wins; a delayed screenshot cannot overwrite it even if
        // the live frame has already completed encoding and the raw gate is empty.
        gate.activate(epoch: 44); _ = gate.request(RecoveryClaim(epoch: 44, requestID: 1))
        let stale = gate.beginSnapshot(at: 0)!
        gate.capturedLiveFrame()
        precondition(!gate.finishSnapshot(stale, at: 100))
        precondition(gate.pendingID == 1)

        // Disconnect/reconnect invalidates stale epoch results but keeps the
        // single physical screenshot slot until its callback actually returns.
        let old = gate.beginSnapshot(at: 100)!
        gate.activate(epoch: nil); gate.activate(epoch: 45)
        _ = gate.request(RecoveryClaim(epoch: 45, requestID: 1))
        precondition(gate.beginSnapshot(at: 200) == nil)
        precondition(!gate.finishSnapshot(old, at: 300))
        let fresh = gate.beginSnapshot(at: 400)!
        precondition(gate.finishSnapshot(fresh, at: 500))

        // A hung framework call gets only one timeout and never allows more
        // concurrent screenshots. A late result is rejected; normal video is free.
        let slow = gate.beginSnapshot(at: 1_000)!
        gate.timeoutSnapshot(slow)
        precondition(gate.beginSnapshot(at: 2_000_000_000) == nil)
        precondition(!gate.finishSnapshot(slow, at: 2_000_000_000))
        precondition(gate.beginSnapshot(at: 2_000_000_001) != nil)

        // Fully idle transport loss has no request epoch or raw frame. Exactly
        // one explicit reconnect snapshot is permitted; normal idle stays idle.
        var reconnect = RecoveryRequestGate()
        precondition(reconnect.beginSnapshot(at: 0) == nil)
        let reconnectToken = reconnect.beginSnapshot(at: 0, allowWithoutRequest: true)!
        precondition(reconnect.beginSnapshot(at: 1, allowWithoutRequest: true) == nil)
        precondition(reconnect.finishSnapshot(reconnectToken, at: 100, allowWithoutRequest: true))
        let superseded = reconnect.beginSnapshot(at: 200, allowWithoutRequest: true)!
        reconnect.capturedLiveFrame()
        precondition(!reconnect.finishSnapshot(superseded, at: 300, allowWithoutRequest: true))

        var cadence = KeyframeCadence()
        precondition(cadence.interval == 30 && cadence.nextSubmission(forceRecovery: false))
        for _ in 1..<20 { precondition(!cadence.nextSubmission(forceRecovery: false)) }
        cadence.setInterval(120)
        for _ in 20..<120 { precondition(!cadence.nextSubmission(forceRecovery: false)) }
        precondition(cadence.nextSubmission(forceRecovery: false))
        precondition(cadence.nextSubmission(forceRecovery: true))
        for _ in 1..<50 { precondition(!cadence.nextSubmission(forceRecovery: false)) }
        cadence.setInterval(30)
        precondition(cadence.nextSubmission(forceRecovery: false))
        print("PASS: HCTL codec/invalid records; negotiation epoch gate;100-request coalescing/250ms rate; idle snapshot; newer live input; reconnect; timeout/late snapshot; safe cadence transitions")
    }
}
