import Foundation
import Darwin

// Synthetic bytes only. No ScreenCaptureKit, capture permission or UI.
@main enum RecoveryTransportProbe {
    static func main() {
        guard CommandLine.arguments.count >= 2, let port = UInt16(CommandLine.arguments[1]) else { exit(2) }
        let enabled = CommandLine.arguments.count < 3 || CommandLine.arguments[2] != "off"
        let queue = DispatchQueue(label: "recovery-transport-test")
        let finished = DispatchSemaphore(value: 0)
        let transport = BoundedTransport(port: port, queue: queue, recoveryEnabled: enabled) { print($0) }
        var sequence: UInt64 = 0
        var cadence = KeyframeCadence()
        var gate = RecoveryRequestGate()
        var force = false
        transport.onRecovery = { event in
            switch event {
            case .active(let epoch): gate.activate(epoch: epoch); cadence.setInterval(120); print("ACTIVE")
            case .inactive: gate.activate(epoch: nil); cadence.setInterval(30); force = true; print("INACTIVE")
            case .request(let claim): _ = gate.request(claim); print("REQUEST \(claim.requestID)")
            }
        }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        let start = DispatchTime.now().uptimeNanoseconds
        timer.schedule(deadline: .now(), repeating: .milliseconds(17))
        timer.setEventHandler {
            if DispatchTime.now().uptimeNanoseconds - start >= 2_500_000_000 {
                timer.cancel(); transport.disconnect(); finished.signal(); return
            }
            let recovery = gate.claim(at: DispatchTime.now().uptimeNanoseconds)
            let key = cadence.nextSubmission(forceRecovery: force || recovery != nil); force = false
            var packet = Data("H264".utf8)
            func put<T: FixedWidthInteger>(_ value: T) {
                var number = value.bigEndian
                withUnsafeBytes(of: &number) { packet.append(contentsOf: $0) }
            }
            put(UInt16(1)); put(UInt16(key ? 1 : 0)); put(UInt32(4)); put(sequence)
            put(DispatchTime.now().uptimeNanoseconds); put(UInt32(0))
            packet.append(contentsOf: [0, 0, 1, key ? 0x65 : 0x41])
            do {
                _ = try transport.write(packet, sequence: sequence, keyframe: key, recovery: recovery)
                sequence += 1
            } catch { force = true; print("WRITE_ERROR \(error)") }
        }
        timer.resume()
        guard finished.wait(timeout: .now() + 5) == .success else { exit(3) }
    }
}
