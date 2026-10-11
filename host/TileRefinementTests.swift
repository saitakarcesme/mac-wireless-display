import AppKit
import ScreenCaptureKit

@main
enum TileRefinementTests {
    static func main() throws {
        func attachments(_ rects: [CGRect]) -> [SCStreamFrameInfo: Any] {
            [.scaleFactor: NSNumber(value: 1), .contentScale: NSNumber(value: 1),
             .contentRect: NSValue(rect: CGRect(x: 0, y: 0, width: 1920, height: 1080)),
             .dirtyRects: rects.map { NSValue(rect: $0) }]
        }
        precondition(TileGrid.damage(attachments([CGRect(x: 1, y: 1, width: 1, height: 1)])).mask == 1)
        precondition(TileGrid.damage(attachments([CGRect(x: 1919, y: 1079, width: 1, height: 1)])).mask == 1 << 47)
        let cross = UInt64(1) | 1 << 1 | 1 << 8 | 1 << 9
        precondition(TileGrid.damage(attachments([CGRect(x: 239.5, y: 179.5, width: 1, height: 1)])).mask == cross)
        precondition(TileGrid.damage(attachments([CGRect(x: 0, y: 0, width: 240, height: 180)])).mask == 1)
        precondition(TileGrid.damage(attachments([])).mask == TileGrid.allMask)
        precondition(TileGrid.damage(nil).fallback)
        var invalid = attachments([CGRect(x: 1, y: 1, width: 1, height: 1)])
        invalid[.contentScale] = 0.5
        precondition(TileGrid.damage(invalid).mask == TileGrid.allMask)
        invalid = attachments([CGRect(x: 1, y: 1, width: 1, height: 1)])
        invalid[.dirtyRects] = [NSValue(point: .zero)]
        precondition(TileGrid.damage(invalid).fallback)
        precondition(TileGrid.cursorSafeMask(1, previous: .zero, current: .zero) == 1)
        precondition(TileGrid.cursorSafeMask(1, previous: .zero, current: CGPoint(x: 1, y: 0)) == TileGrid.allMask)
        precondition(TileGrid.cursorSafeMask(1, previous: .zero, current: nil) == TileGrid.allMask)

        var state = TileTracker()
        let baseline = state.capture(mask: TileGrid.allMask, at: 0)
        state.sent(baseline, sequence: 0)
        _ = state.capture(mask: 1 << 2, at: 10) // This raw frame is replaced before encoding.
        let latest = state.capture(mask: 1 << 5, at: 20)
        precondition(TileGrid.changedMask(latest, since: baseline) == (1 << 2 | 1 << 5))
        precondition(TileGrid.changedMask(latest, since: nil) == TileGrid.allMask)
        // Pixels changed after the sent baseline cannot become RGB candidates yet.
        precondition(state.candidates(at: 2_000_000_000).count == 46)
        state.sent(latest, sequence: 1)
        precondition(state.candidates(at: 2_000_000_000).count == 48)

        var caret = TileTracker()
        let first = caret.capture(mask: TileGrid.allMask, at: 0)
        caret.sent(first, sequence: 0)
        let blinking = caret.capture(mask: 1, at: 500_000_000)
        caret.sent(blinking, sequence: 1)
        let settled = caret.candidates(at: 800_000_000)
        precondition(settled.count == 47 && !settled.contains(where: { $0.tile == 0 }))
        precondition(caret.candidates(at: 100).isEmpty) // Future motion time must not underflow.
        let unaffected = settled.first!
        let next = caret.capture(mask: 1, at: 1_000_000_000)
        caret.sent(next, sequence: 2)
        precondition(caret.valid(unaffected))
        for candidate in settled { caret.accepted(candidate) }
        precondition(caret.candidates(at: 1_100_000_000).isEmpty) // Caret causes no repeated RGB capture.
        let changed = caret.capture(mask: 1 << UInt64(unaffected.tile), at: 1_200_000_000)
        caret.sent(changed, sequence: 3)
        precondition(!caret.valid(unaffected))
        caret.restartTransport()
        precondition(caret.candidates(at: 3_000_000_000).isEmpty)
        caret.sent(changed, sequence: 4)
        precondition(caret.candidates(at: 3_000_000_000).count == 48)
        for candidate in caret.candidates(at: 3_000_000_000) { caret.accepted(candidate) }
        precondition(caret.candidates(at: 3_000_000_000).isEmpty)
        caret.invalidateRefinement() // mpv restarted while video transport stayed open.
        precondition(caret.candidates(at: 3_000_000_000).count == 48)

        var full = Data(count: 1920 * 1080 * 4)
        full.withUnsafeMutableBytes { storage in
            let bytes = storage.bindMemory(to: UInt8.self)
            for y in 0..<1080 { for x in 0..<1920 {
                let offset = (y * 1920 + x) * 4
                bytes[offset] = UInt8(x % 256); bytes[offset + 1] = UInt8(y % 256)
                bytes[offset + 2] = UInt8((x + y) % 256); bytes[offset + 3] = 255
            } }
        }
        for tile in 0..<48 {
            let data = try TileGrid.pixels(full, tile: tile)
            precondition(data.count == 172_800)
            for (px, py) in [(0, 0), (239, 179)] {
                let x = tile % 8 * 240 + px, y = tile / 8 * 180 + py
                let offset = (py * 240 + px) * 4
                precondition(Array(data[offset..<(offset + 4)]) == [UInt8(x % 256), UInt8(y % 256), UInt8((x + y) % 256), 255])
            }
        }
        print("PASS: damage boundaries/fallback; skipped raw damage union; caret-safe candidates; transport reset; every packed tile orientation/stride")
    }
}
