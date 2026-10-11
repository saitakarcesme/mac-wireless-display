import Foundation
import Darwin

// Synthetic bytes only. No ScreenCaptureKit, capture permission or UI.
// Completion is semantic, not an FPS assertion. The independent main-thread
// watchdog still bounds a stalled transport or a starved dispatch queue.
@main enum RecoveryTransportProbe {
    static func main() {
        let args = CommandLine.arguments
        guard args.count >= 2, let port = UInt16(args[1]) else { exit(2) }
        let enabled = args.count < 3 || args[2] != "off"
        let completion = args.count < 4 ? "frames" : args[3]
        let frameTarget = args.count < 5 ? 96 : Int(args[4]) ?? 0
        guard ["frames", "reset"].contains(completion), (64...512).contains(frameTarget) else { exit(2) }
        setbuf(stdout, nil)
        let queue = DispatchQueue(label: "recovery-transport-test")
        let finished = DispatchSemaphore(value: 0)
        let start = DispatchTime.now().uptimeNanoseconds
        var finish: ((String) -> Void)?
        let transport = BoundedTransport(port: port, queue: queue, recoveryEnabled: enabled) { message in
            print(message)
            if completion == "reset" && message.hasPrefix("recovery control reset reason=") {
                // failControl must finish closing its descriptor before teardown.
                queue.async { finish?("control-reset") }
            }
        }
        var sequence: UInt64 = 0
        var generation: UInt64 = 0
        var connectionFrames = 0
        var writeErrors = 0
        var cadence = KeyframeCadence()
        var gate = RecoveryRequestGate()
        var force = false
        var ended = false
        transport.onRecovery = { event in
            switch event {
            case .active(let epoch): gate.activate(epoch: epoch); cadence.setInterval(120); print("ACTIVE")
            case .inactive: gate.activate(epoch: nil); cadence.setInterval(30); force = true; print("INACTIVE")
            case .request(let claim): _ = gate.request(claim); print("REQUEST \(claim.requestID)")
            }
        }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        finish = { reason in
            guard !ended else { return }
            ended = true
            timer.cancel()
            transport.disconnect()
            let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start) / 1e9
            print("COMPLETE completion=\(completion) reason=\(reason) successful_frames=\(sequence) connection_frames=\(connectionFrames) write_errors=\(writeErrors) elapsed_s=\(elapsed)")
            finished.signal()
        }
        timer.schedule(deadline: .now(), repeating: .milliseconds(17))
        timer.setEventHandler {
            guard !ended else { return }
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
                if generation != transport.connectionGeneration {
                    generation = transport.connectionGeneration
                    connectionFrames = 0
                }
                connectionFrames += 1
                if completion == "frames" && connectionFrames >= frameTarget {
                    finish?("frame-target")
                }
            } catch { force = true; writeErrors += 1; print("WRITE_ERROR \(error)") }
        }
        timer.resume()
        guard finished.wait(timeout: .now() + 30) == .success else {
            fputs("PROBE_TIMEOUT: semantic completion missing after30s; queue or transport may be stalled\n", stderr)
            exit(3)
        }
    }
}
