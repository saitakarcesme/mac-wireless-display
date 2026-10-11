import Foundation

@main
enum KeyframeCadenceTests {
    static func main() throws {
        for interval in [30, 120, 600] {
            var policy = KeyframeCadence(interval: interval)
            let keys = (0..<(interval * 3 + 1)).filter { _ in policy.nextSubmission(forceRecovery: false) }
            precondition(keys == [0, interval, interval * 2, interval * 3])
        }
        // A failure/recovery request must override a long interval and reset
        // the fallback count. Idle time does not consume a submission slot.
        var policy = KeyframeCadence(interval: 120)
        var keys: [Int] = []
        for index in 0...170 {
            if policy.nextSubmission(forceRecovery: index == 50) { keys.append(index) }
        }
        precondition(keys == [0, 50, 170])
        let valid = try KeyframeCadence.validatedInterval(NSNumber(value: 120))
        precondition(valid == 120)
        for invalid: Any in [true, false, 29, 601, 120.5, "120", NSNumber(value: Double.nan), NSNumber(value: Double.infinity)] {
            do {
                _ = try KeyframeCadence.validatedInterval(invalid)
                preconditionFailure("accepted invalid interval: \(invalid)")
            } catch KeyframeCadence.InvalidInterval.expectedInteger30Through600 {}
        }
        print("PASS: cadence bounds, exact fallback intervals, recovery override/reset, and strict config parsing")
    }
}
