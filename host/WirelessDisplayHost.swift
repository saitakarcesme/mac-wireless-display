import AppKit
import ScreenCaptureKit
import VideoToolbox
import CoreMedia
import CoreVideo
import Darwin

private let loggingLock = NSLock()
private let logDateFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}()
private let logFile: FileHandle? = {
    let directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/mac-wireless-display")
    try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let file = directory.appendingPathComponent("h264-host.log")
    if !FileManager.default.fileExists(atPath: file.path) {
        FileManager.default.createFile(atPath: file.path, contents: nil,
                                      attributes: [.posixPermissions: NSNumber(value: 0o600)])
    }
    guard let handle = try? FileHandle(forWritingTo: file) else { return nil }
    _ = try? handle.seekToEnd()
    return handle
}()

private func log(_ message: String) {
    loggingLock.lock()
    defer { loggingLock.unlock() }
    let data = Data((logDateFormatter.string(from: Date()) + " " + message + "\n").utf8)
    FileHandle.standardError.write(data)
    try? logFile?.write(contentsOf: data)
}

private struct Options {
    var displayID: CGDirectDisplayID = 0
    var port: UInt16 = 16010
    var fps: Int32 = 60
    // Explicit experimental SCK throttle, independent of encoder ExpectedFrameRate.
    // Nil preserves existing behavior; zero requests the display's native cadence.
    var captureFPS: Int32?
    var captureFrameInterval: CMTime {
        let rate = captureFPS ?? fps
        return rate == 0 ? .zero : CMTime(value: 1, timescale: rate)
    }
    var bitrate: Int = 6_000_000
    // Keep the proven fallback until reverse receiver recovery is implemented.
    var keyframeInterval = KeyframeCadence.defaultInterval
    // Explicitly opt in before testing native/120Hz capture. ExpectedFrameRate
    // is only an encoder hint; this policy actually bounds submissions.
    var capEncoderFPS = false
    // Separate experiment: keep asyncAfter unless explicitly enabled with cap.
    var strictPacerTimer = false
    var copyFrames = false
    var refineStatic = false
    var refineTiles = false
    static func parse() throws -> Options {
        var options = Options()
        let settingsFile = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/mac-wireless-display/h264-settings.json")
        if FileManager.default.fileExists(atPath: settingsFile.path) {
            guard let settings = try JSONSerialization.jsonObject(with: Data(contentsOf: settingsFile)) as? [String: Any] else {
                throw Failure("h264-settings.json must contain a JSON object")
            }
            if let value = settings["bitrate"] {
                guard let number = value as? NSNumber, (500_000...30_000_000).contains(number.intValue) else {
                    throw Failure("settings bitrate must be 500000...30000000")
                }
                options.bitrate = number.intValue
            }
            if let value = settings["fps"] {
                guard let number = value as? NSNumber, (1...60).contains(number.intValue) else {
                    throw Failure("settings fps must be 1...60")
                }
                options.fps = number.int32Value
            }
            if let value = settings["captureFPS"] {
                guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      (0...120).contains(number.intValue), number.doubleValue == Double(number.intValue) else {
                    throw Failure("settings captureFPS must be an integer0...120 (0=native)")
                }
                options.captureFPS = number.int32Value
            }
            if let value = settings["keyframeInterval"] {
                do { options.keyframeInterval = try KeyframeCadence.validatedInterval(value) }
                catch { throw Failure("settings keyframeInterval must be an integer30...600") }
            }
            if let value = settings["capEncoderFPS"] {
                guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
                    throw Failure("settings capEncoderFPS must be true or false")
                }
                options.capEncoderFPS = number.boolValue
            }
            if let value = settings["strictPacerTimer"] {
                guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
                    throw Failure("settings strictPacerTimer must be true or false")
                }
                options.strictPacerTimer = number.boolValue
            }
            if let value = settings["refineStatic"] {
                guard let enabled = value as? Bool else { throw Failure("settings refineStatic must be true or false") }
                options.refineStatic = enabled
            }
            if let value = settings["refineTiles"] {
                guard let enabled = value as? Bool else { throw Failure("settings refineTiles must be true or false") }
                options.refineTiles = enabled
            }
            if let value = settings["copyFrames"] {
                guard let enabled = value as? Bool else {
                    throw Failure("settings copyFrames must be true or false")
                }
                options.copyFrames = enabled
            }
        }
        let arguments = Array(CommandLine.arguments.dropFirst())
        var index = 0
        while index < arguments.count {
            guard index + 1 < arguments.count else { throw Failure("missing argument value") }
            let name = arguments[index], value = arguments[index + 1]
            switch name {
            case "--display-id":
                guard let parsed = UInt32(value), parsed > 0 else { throw Failure("invalid display ID") }
                options.displayID = parsed
            case "--port":
                guard let parsed = UInt16(value), parsed > 0 else { throw Failure("invalid port") }
                options.port = parsed
            case "--fps":
                guard let parsed = Int32(value), (1...60).contains(parsed) else { throw Failure("FPS must be 1...60") }
                options.fps = parsed
            case "--capture-fps":
                guard let parsed = Int32(value), (0...120).contains(parsed) else { throw Failure("capture FPS must be0...120 (0=native)") }
                options.captureFPS = parsed
            case "--bitrate":
                guard let parsed = Int(value), (500_000...30_000_000).contains(parsed) else { throw Failure("bitrate must be 500000...30000000") }
                options.bitrate = parsed
            case "--keyframe-interval":
                guard let parsed = Int(value), KeyframeCadence.allowedIntervals.contains(parsed) else {
                    throw Failure("keyframe interval must be30...600 submitted frames")
                }
                options.keyframeInterval = parsed
            case "--copy-frames":
                guard value == "true" || value == "false" else { throw Failure("copy-frames must be true or false") }
                options.copyFrames = value == "true"
            case "--cap-encoder-fps":
                guard value == "true" || value == "false" else { throw Failure("cap-encoder-fps must be true or false") }
                options.capEncoderFPS = value == "true"
            case "--strict-pacer-timer":
                guard value == "true" || value == "false" else { throw Failure("strict-pacer-timer must be true or false") }
                options.strictPacerTimer = value == "true"
            default: throw Failure("unknown argument \(name)")
            }
            index += 2
        }
        if options.displayID == 0 {
            let path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/mac-wireless-display/display-id")
            if let text = try? String(contentsOf: path, encoding: .utf8),
               let parsed = UInt32(text.trimmingCharacters(in: .whitespacesAndNewlines)), parsed > 0 {
                options.displayID = parsed
            }
        }
        guard options.displayID > 0 else { throw Failure("--display-id is required when ~/.config/mac-wireless-display/display-id is missing or invalid") }
        return options
    }
}

private struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

/// SCK displayTime is mach absolute ticks, whereas the wire uses uptime ns.
/// Convert by quotient/remainder so long uptimes cannot overflow ticks * numer.
private enum CaptureClock {
    static let timebase: mach_timebase_info_data_t = {
        var value = mach_timebase_info_data_t()
        mach_timebase_info(&value)
        return value
    }()

    static func nanoseconds(fromMachTicks ticks: UInt64, numer: UInt32 = timebase.numer,
                            denom: UInt32 = timebase.denom) -> UInt64? {
        guard numer > 0, denom > 0 else { return nil }
        let divisor = UInt64(denom), multiplier = UInt64(numer)
        let (whole, multiplyOverflow) = (ticks / divisor).multipliedReportingOverflow(by: multiplier)
        guard !multiplyOverflow else { return nil }
        // Each remainder and multiplier is at most UInt32.max, so this product fits.
        let fraction = (ticks % divisor) * multiplier / divisor
        let (result, addOverflow) = whole.addingReportingOverflow(fraction)
        return addOverflow ? nil : result
    }

    struct Observation {
        let ticks: UInt64?
        let nanoseconds: UInt64?
        let reason: String
        let type: String
    }

    static func observe(_ value: Any?, callbackNS: UInt64) -> Observation {
        guard let value else { return Observation(ticks: nil, nanoseconds: nil, reason: "missing", type: "nil") }
        let type = String(describing: Swift.type(of: value))
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "C", "s", "S", "i", "I", "l", "L", "q", "Q"].contains(String(cString: number.objCType)),
              number.compare(NSNumber(value: 0)) == .orderedDescending else {
            return Observation(ticks: nil, nanoseconds: nil, reason: "invalid-type", type: type)
        }
        let ticks = number.uint64Value
        guard let result = nanoseconds(fromMachTicks: ticks) else {
            return Observation(ticks: ticks, nanoseconds: nil, reason: "overflow", type: String(cString: number.objCType))
        }
        return Observation(ticks: ticks, nanoseconds: result,
                           reason: result > callbackNS ? "future" : "elapsed", type: String(cString: number.objCType))
    }
}

private struct OwnedRawFrame {
    let image: CVPixelBuffer
    let presentationTime: CMTime
    let captureTimeNS: UInt64
    let tileVersions: [UInt64]?
}

private final class FrameContext {
    // Strongly own the input through encode and write, then release it before
    // allowing the next encode. Zero-copy inputs remain valid SCK surfaces.
    var raw: OwnedRawFrame?
    let submittedNS = DispatchTime.now().uptimeNanoseconds
    let captureTimeNS: UInt64
    let tileVersions: [UInt64]?
    init(_ raw: OwnedRawFrame) {
        self.raw = raw; self.captureTimeNS = raw.captureTimeNS; self.tileVersions = raw.tileVersions
    }
}

private struct Metrics {
    var callbacks = 0, complete = 0, encoded = 0, written = 0
    var skipped = 0, encodeErrors = 0, writeErrors = 0, blockedWrites = 0
    var bytes = 0, encodeTimeNS: UInt64 = 0, writeTimeNS: UInt64 = 0
    var copied = 0, copyErrors = 0, copyTimeNS: UInt64 = 0
    var zeroCopyFrames = 0
    var pendingReplaced = 0, pendingDrained = 0, pendingWaitNS: UInt64 = 0
    var pendingMaxWaitNS: UInt64 = 0
    var drainWake = DrainWakeMetrics()
    var captureStatuses: [Int: Int] = [:]
    var displayTimeFrames = 0, callbackTimeFrames = 0
    var captureLagSumNS = 0.0, captureLagMaxNS = 0.0, captureLagMinNS = 0.0
    var displayTimeReasons: [String: Int] = [:]
    var displayStepCount = 0, displayStepRepeat = 0, displayStepReverse = 0
    var displayStepBelow12MS = 0, displayStepAbove25MS = 0
    var displayStepSumNS = 0.0, displayStepMaxNS: UInt64 = 0
    // Normally ~120 values per two-second interval; bounded even if reporting stalls.
    var captureLagSamplesNS: [Double] = []
    var displayStepSamplesNS: [UInt64] = []

    mutating func recordDisplayStep(previous: UInt64?, current: UInt64?) {
        guard let previous, let current else { return }
        guard current > previous else {
            if current == previous { displayStepRepeat += 1 } else { displayStepReverse += 1 }
            return
        }
        let step = current - previous
        displayStepCount += 1; displayStepSumNS += Double(step)
        displayStepMaxNS = max(displayStepMaxNS, step)
        if step < 12_000_000 { displayStepBelow12MS += 1 }
        if step > 25_000_000 { displayStepAbove25MS += 1 }
        if displayStepSamplesNS.count < 256 { displayStepSamplesNS.append(step) }
        else { displayStepSamplesNS[(displayStepCount - 1) % 256] = step }
    }

    mutating func recordCaptureTime(_ observation: CaptureClock.Observation, callbackNS: UInt64) {
        displayTimeReasons[observation.reason, default: 0] += 1
        guard let displayNS = observation.nanoseconds else { callbackTimeFrames += 1; return }
        // displayTime can be later than delivery on this virtual display. Keep
        // the signed delta as a diagnostic; it is not a capture-latency proof.
        let lag = callbackNS >= displayNS ? Double(callbackNS - displayNS) : -Double(displayNS - callbackNS)
        displayTimeFrames += 1
        captureLagSumNS += lag
        captureLagMaxNS = displayTimeFrames == 1 ? lag : max(captureLagMaxNS, lag)
        captureLagMinNS = displayTimeFrames == 1 ? lag : min(captureLagMinNS, lag)
        if captureLagSamplesNS.count < 256 {
            captureLagSamplesNS.append(lag)
        } else {
            captureLagSamplesNS[(displayTimeFrames - 1) % 256] = lag
        }
    }
}

/// Own the encoder's input surfaces independently of ScreenCaptureKit's pool.
/// VideoToolbox may retain a source surface after its output callback finishes.
private final class EncoderPixelPool {
    private let pool: CVPixelBufferPool
    private let allocationLimit = [kCVPixelBufferPoolAllocationThresholdKey: 8] as CFDictionary

    init() throws {
        var output: CVPixelBufferPool?
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferWidthKey: 1920, kCVPixelBufferHeightKey: 1080,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [String: Any]
        ]
        let result = CVPixelBufferPoolCreate(kCFAllocatorDefault,
            [kCVPixelBufferPoolMinimumBufferCountKey: 4] as CFDictionary,
            attributes as CFDictionary, &output)
        guard result == kCVReturnSuccess, let output else { throw Failure("owned pixel pool creation status=\(result)") }
        pool = output
    }

    func copy(_ source: CVPixelBuffer) throws -> CVPixelBuffer {
        guard CVPixelBufferGetPixelFormatType(source) == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
              CVPixelBufferGetWidth(source) == 1920, CVPixelBufferGetHeight(source) == 1080,
              CVPixelBufferGetPlaneCount(source) == 2 else { throw Failure("unexpected captured pixel format or size") }
        var output: CVPixelBuffer?
        let result = CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(kCFAllocatorDefault, pool, allocationLimit, &output)
        guard result == kCVReturnSuccess, let output else { throw Failure("owned pixel pool allocation status=\(result)") }
        guard CVPixelBufferLockBaseAddress(source, .readOnly) == kCVReturnSuccess else { throw Failure("source pixel lock failed") }
        defer { CVPixelBufferUnlockBaseAddress(source, .readOnly) }
        guard CVPixelBufferLockBaseAddress(output, []) == kCVReturnSuccess else { throw Failure("destination pixel lock failed") }
        defer { CVPixelBufferUnlockBaseAddress(output, []) }
        for plane in 0..<2 {
            guard let from = CVPixelBufferGetBaseAddressOfPlane(source, plane),
                  let to = CVPixelBufferGetBaseAddressOfPlane(output, plane) else { throw Failure("missing NV12 plane") }
            let height = CVPixelBufferGetHeightOfPlane(source, plane)
            let rowBytes = CVPixelBufferGetWidthOfPlane(source, plane) * (plane == 0 ? 1 : 2)
            let sourceStride = CVPixelBufferGetBytesPerRowOfPlane(source, plane)
            let destinationStride = CVPixelBufferGetBytesPerRowOfPlane(output, plane)
            guard rowBytes <= sourceStride, rowBytes <= destinationStride else { throw Failure("invalid NV12 row stride") }
            if sourceStride == destinationStride && rowBytes == sourceStride {
                memcpy(to, from, rowBytes * height)
            } else {
                for row in 0..<height {
                    memcpy(to.advanced(by: row * destinationStride), from.advanced(by: row * sourceStride), rowBytes)
                }
            }
        }
        CVBufferPropagateAttachments(source, output)
        return output
    }
}

/// All socket access occurs on the output queue. The socket never leaves loopback.
private final class BoundedTransport {
    private var descriptor: Int32 = -1
    private(set) var connectionGeneration: UInt64 = 0
    private let port: UInt16
    init(port: UInt16) { self.port = port }
    deinit { disconnect() }
    func disconnect() {
        if descriptor >= 0 { Darwin.close(descriptor); descriptor = -1 }
    }
    private func connectIfNeeded() throws {
        if descriptor >= 0 { return }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure("socket errno=\(errno)") }
        descriptor = fd
        var yes: Int32 = 1, buffer: Int32 = 65_536
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer, socklen_t(MemoryLayout.size(ofValue: buffer)))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        // A loopback connect is immediate or refused; no external network connect here.
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard result == 0 else { let error = errno; disconnect(); throw Failure("connect errno=\(error)") }
        let current = fcntl(fd, F_GETFL)
        guard current >= 0, fcntl(fd, F_SETFL, current | O_NONBLOCK) == 0 else {
            disconnect(); throw Failure("nonblocking socket configuration failed")
        }
        connectionGeneration &+= 1
        log("transport connected target=127.0.0.1:\(port)")
    }
    func write(_ data: Data) throws -> (durationNS: UInt64, blocked: Bool) {
        try connectIfNeeded()
        let start = DispatchTime.now().uptimeNanoseconds
        let deadline = start + 250_000_000
        var blocked = false
        do {
            try data.withUnsafeBytes { buffer in
                guard let base = buffer.baseAddress else { throw Failure("empty packet") }
                var offset = 0
                while offset < buffer.count {
                    let now = DispatchTime.now().uptimeNanoseconds
                    guard now < deadline else { throw Failure("write deadline 250ms") }
                    let count = Darwin.send(descriptor, base.advanced(by: offset), buffer.count - offset, 0)
                    if count > 0 { offset += count; continue }
                    if count < 0 && errno == EINTR { continue }
                    guard count < 0 && (errno == EWOULDBLOCK || errno == EAGAIN) else {
                        throw Failure("send errno=\(errno)")
                    }
                    blocked = true
                    var pollFD = pollfd(fd: descriptor, events: Int16(POLLOUT), revents: 0)
                    let remaining = Int32(max(1, min(250, (deadline - now) / 1_000_000)))
                    let result = poll(&pollFD, 1, remaining)
                    if result < 0 && errno == EINTR { continue }
                    guard result > 0, (pollFD.revents & Int16(POLLERR | POLLHUP | POLLNVAL)) == 0 else {
                        throw Failure("write poll timeout or disconnect")
                    }
                }
            }
            return (DispatchTime.now().uptimeNanoseconds - start, blocked)
        } catch { disconnect(); throw error }
    }
}

private final class DisplayHost: NSObject, SCStreamOutput, SCStreamDelegate {
    private let options: Options
    private let captureQueue = DispatchQueue(label: "wireless-display.capture", qos: .userInteractive)
    private let outputQueue = DispatchQueue(label: "wireless-display.output", qos: .userInteractive)
    private let stateLock = NSLock()
    private let transport: BoundedTransport
    private var session: VTCompressionSession?
    private var pixelPool: EncoderPixelPool?
    private var stream: SCStream?
    private var refinement: StaticRefinement?
    private var tileRefinement: TileRefinement?
    // Output queue only. A failed write discards the previous damage baseline.
    private var lastSentTileVersions: [UInt64]?
    private var timer: DispatchSourceTimer?
    // Gate and retry scheduling are confined to captureQueue.
    private var gate = LatestFrameGate<OwnedRawFrame>()
    private var submissionPacer: SubmissionPacer
    private var drainWakeup = DrainWakeup()
    private var scheduledDrain: DispatchWorkItem?
    private var scheduledDrainTimer: DispatchSourceTimer?
    private var captureStopped = false
    private var lastSubmittedPTS = CMTime.invalid
    private var needsKeyFrame = true
    private var keyframeCadence: KeyframeCadence
    private var reconnectAfterNS: UInt64 = 0
    private var sequence: UInt64 = 0
    private var metrics = Metrics()
    private var lastMetricsNS = DispatchTime.now().uptimeNanoseconds
    private var activity: NSObjectProtocol?
    private var clockDiagnostics = 0
    private var previousCompleteDisplayNS: UInt64?

    init(options: Options) {
        self.options = options
        transport = BoundedTransport(port: options.port)
        keyframeCadence = KeyframeCadence(interval: options.keyframeInterval)
        submissionPacer = SubmissionPacer(enabled: options.capEncoderFPS, fps: options.fps)
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first(where: { $0.displayID == options.displayID }) else {
            throw Failure("requested display not present; available IDs=\(content.displays.map(\.displayID))")
        }
        guard display.width == 1920 && display.height == 1080 else {
            throw Failure("display must be 1920x1080, actual=\(display.width)x\(display.height)")
        }
        try makeEncoder()
        pixelPool = options.copyFrames ? try EncoderPixelPool() : nil
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep, .latencyCritical],
                                                        reason: "Low latency wireless display capture")
        let stream = try makeStream(display: display)
        self.stream = stream
        if options.refineTiles {
            tileRefinement = TileRefinement(display: display, report: log)
        } else if options.refineStatic {
            refinement = StaticRefinement(display: display, report: log)
        }
        try await stream.startCapture()
        let timer = DispatchSource.makeTimerSource(queue: captureQueue)
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler { [weak self] in self?.reportMetrics() }
        self.timer = timer
        timer.resume()
        log("capture started display=\(display.displayID) width=1920 height=1080 fps=\(options.fps) capture_fps_limit=\(options.captureFPS ?? options.fps) encoder_fps_cap=\(options.capEncoderFPS ? String(options.fps) : "off") drain_timer=\(options.capEncoderFPS && options.strictPacerTimer ? "strict-zero-leeway" : "asyncAfter") bitrate=\(options.bitrate) cursor=true queueDepth=5 input=\(options.copyFrames ? "owned-copy" : "zero-copy") retainedCaptureSurfaceLimit=3 pendingRawLimit=1 color=sRGB-transfer-BT709-matrix-video-range")
    }

    private func makeStream(display: SCDisplay) throws -> SCStream {
        let configuration = SCStreamConfiguration()
        configuration.width = 1920
        configuration.height = 1080
        configuration.minimumFrameInterval = options.captureFrameInterval
        configuration.queueDepth = 5
        configuration.showsCursor = true
        configuration.capturesAudio = false
        configuration.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        configuration.colorSpaceName = CGColorSpace.sRGB
        configuration.colorMatrix = kCVImageBufferYCbCrMatrix_ITU_R_709_2
        configuration.captureDynamicRange = .SDR
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: captureQueue)
        return stream
    }

    private func makeEncoder() throws {
        let specification: [CFString: Any] = [
            kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true,
            kVTVideoEncoderSpecification_EnableLowLatencyRateControl: true
        ]
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferWidthKey: 1920, kCVPixelBufferHeightKey: 1080,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [String: Any]
        ]
        let status = VTCompressionSessionCreate(allocator: kCFAllocatorDefault, width: 1920, height: 1080,
            codecType: kCMVideoCodecType_H264, encoderSpecification: specification as CFDictionary,
            imageBufferAttributes: attributes as CFDictionary, compressedDataAllocator: nil,
            outputCallback: { refcon, frameRefcon, status, flags, sample in
                guard let refcon, let frameRefcon else { return }
                let host = Unmanaged<DisplayHost>.fromOpaque(refcon).takeUnretainedValue()
                let frame = Unmanaged<FrameContext>.fromOpaque(frameRefcon).takeRetainedValue()
                host.encoded(frame: frame, status: status, flags: flags, sample: sample)
            }, refcon: Unmanaged.passUnretained(self).toOpaque(), compressionSessionOut: &session)
        guard status == noErr, let session else { throw Failure("hardware encoder creation failed status=\(status)") }
        func set(_ key: CFString, _ value: CFTypeRef, required: Bool = false) throws {
            let result = VTSessionSetProperty(session, key: key, value: value)
            if result != noErr { log("encoder property=\(key) status=\(result)"); if required { throw Failure("required encoder property failed \(key)") } }
        }
        try set(kVTCompressionPropertyKey_RealTime, kCFBooleanTrue, required: true)
        try set(kVTCompressionPropertyKey_AllowTemporalCompression, kCFBooleanTrue, required: true)
        try set(kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse, required: true)
        try set(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel, required: true)
        try set(kVTCompressionPropertyKey_ExpectedFrameRate, NSNumber(value: options.fps))
        try set(kVTCompressionPropertyKey_MaxFrameDelayCount, NSNumber(value: 0))
        // Do not set ReferenceBufferCount=1: Apple's H.264 encoder on macOS 26
        // drops every other frame with that setting (Chromium bug 450596068).
        try set(kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, kCFBooleanTrue)
        try set(kVTCompressionPropertyKey_AverageBitRate, NSNumber(value: options.bitrate), required: true)
        // A 100ms envelope caps bursts while allowing 50% headroom over the mean target.
        let limitBytes = Double(options.bitrate) / 8.0 * 0.1 * 1.5
        try set(kVTCompressionPropertyKey_DataRateLimits, [NSNumber(value: limitBytes), NSNumber(value: 0.1)] as CFArray)
        try set(kVTCompressionPropertyKey_ColorPrimaries, kCVImageBufferColorPrimaries_ITU_R_709_2)
        try set(kVTCompressionPropertyKey_TransferFunction, kCVImageBufferTransferFunction_sRGB)
        try set(kVTCompressionPropertyKey_YCbCrMatrix, kCVImageBufferYCbCrMatrix_ITU_R_709_2)
        let prepared = VTCompressionSessionPrepareToEncodeFrames(session)
        guard prepared == noErr else { throw Failure("encoder preparation failed status=\(prepared)") }
        let hardware = UnsafeMutablePointer<CFTypeRef?>.allocate(capacity: 1)
        hardware.initialize(to: nil)
        defer { hardware.deinitialize(count: 1); hardware.deallocate() }
        let query = VTSessionCopyProperty(session, key: kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,
                                          allocator: kCFAllocatorDefault, valueOut: hardware)
        var hardwareConfirmed = query == noErr && (hardware.pointee as? NSNumber)?.boolValue == true
        var encoderID = "unknown"
        let encoderValue = UnsafeMutablePointer<CFTypeRef?>.allocate(capacity: 1)
        encoderValue.initialize(to: nil)
        defer { encoderValue.deinitialize(count: 1); encoderValue.deallocate() }
        if VTSessionCopyProperty(session, key: kVTCompressionPropertyKey_EncoderID,
                                 allocator: kCFAllocatorDefault, valueOut: encoderValue) == noErr,
           let identifier = encoderValue.pointee as? String {
            encoderID = identifier
            // Apple's RTVC hardware wrapper does not expose UsingHardware... .
            // Chromium uses this exact known hardware encoder-ID fallback too.
            if query != noErr && identifier == "com.apple.videotoolbox.videoencoder.h264.rtvc" {
                hardwareConfirmed = true
            }
        }
        guard hardwareConfirmed else {
            throw Failure("hardware encoding could not be confirmed status=\(query)")
        }
        log("encoder ready codec=H264 hardware=true encoderID=\(encoderID) hardwarePropertyStatus=\(query) profile=HighAuto lowLatency=true frameReordering=false keyframeInterval=\(options.keyframeInterval) receiverRecoveryControl=false")
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        guard !captureStopped else { return } // Invoked only on captureQueue.
        let callbackMachTicks = mach_absolute_time()
        let callbackNS = CaptureClock.nanoseconds(fromMachTicks: callbackMachTicks)
            ?? DispatchTime.now().uptimeNanoseconds
        let attachments = sampleBuffer.isValid
            ? CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]] : nil
        let rawStatus = attachments?.first?[.status] as? Int ?? -1
        stateLock.lock()
        guard stream === self.stream else { stateLock.unlock(); return }
        metrics.callbacks += 1
        metrics.captureStatuses[rawStatus, default: 0] += 1
        stateLock.unlock()
        guard sampleBuffer.isValid,
              SCFrameStatus(rawValue: rawStatus) == .complete,
              let capturedImage = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let now = callbackNS
        let displayObservation = CaptureClock.observe(attachments?.first?[.displayTime], callbackNS: callbackNS)
        if clockDiagnostics < 6 {
            clockDiagnostics += 1
            let deltaMS = displayObservation.nanoseconds.map { value in
                value <= callbackNS ? Double(callbackNS - value) / 1e6 : -Double(value - callbackNS) / 1e6
            }
            log("display-clock source=\(displayObservation.reason) type=\(displayObservation.type) ticks=\(displayObservation.ticks.map(String.init) ?? "nil") callback_ticks=\(callbackMachTicks) timebase=\(CaptureClock.timebase.numer)/\(CaptureClock.timebase.denom) callback_minus_display_ms=\(deltaMS.map { String(format: "%.3f", $0) } ?? "nil") wire_source=callback")
        }
        stateLock.withLock {
            metrics.complete += 1
            metrics.recordCaptureTime(displayObservation, callbackNS: callbackNS)
            metrics.recordDisplayStep(previous: previousCompleteDisplayNS, current: displayObservation.nanoseconds)
        }
        if let displayNS = displayObservation.nanoseconds { previousCompleteDisplayNS = displayNS }
        refinement?.frameCaptured()
        let tileVersions = tileRefinement?.frameCaptured(attachments?.first)
        do {
            let image: CVPixelBuffer
            if options.copyFrames {
                guard let pixelPool else { throw Failure("copy mode requires an owned pixel pool") }
                let copyStart = DispatchTime.now().uptimeNanoseconds
                image = try pixelPool.copy(capturedImage)
                let copyDuration = DispatchTime.now().uptimeNanoseconds - copyStart
                stateLock.withLock { metrics.copied += 1; metrics.copyTimeNS += copyDuration }
            } else {
                // One active frame + one latest pending frame retain at most two
                // surfaces; replacement briefly adds this incoming third one.
                // No CPU mapping/locking of the capture surface is needed.
                image = capturedImage
                stateLock.withLock { metrics.zeroCopyFrames += 1 }
            }
            let raw = OwnedRawFrame(image: image,
                                    presentationTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer),
                                    captureTimeNS: callbackNS, tileVersions: tileVersions)
            let retryAfter = stateLock.withLock { reconnectAfterNS }
            let replaced = gate.offer(raw, at: now,
                                      blockedUntil: submissionPacer.earliestSubmission(retryAfter: retryAfter))
            stateLock.withLock {
                if replaced { metrics.pendingReplaced += 1; metrics.skipped += 1 }
            }
            drainPendingFrame()
        } catch {
            stateLock.withLock { metrics.copyErrors += 1 }
            log("capture copy failed \(error)")
        }
    }

    private func drainPendingFrame() {
        dispatchPrecondition(condition: .onQueue(captureQueue))
        guard !captureStopped, gate.hasPending, !gate.inFlight, let session else {
            cancelScheduledDrain()
            return
        }
        let now = DispatchTime.now().uptimeNanoseconds
        let retryAfter = stateLock.withLock { reconnectAfterNS }
        let earliest = submissionPacer.earliestSubmission(retryAfter: retryAfter)
        if now < earliest {
            scheduleDrain(at: earliest)
            return
        }
        cancelScheduledDrain()
        guard let pending = gate.takeIfReady(at: now, blockedUntil: earliest) else { return }
        let forceKey = stateLock.withLock { () -> Bool in
            if pending.wasDeferred {
                let wait = now - pending.offeredAtNS
                metrics.pendingDrained += 1
                metrics.pendingWaitNS += wait
                metrics.pendingMaxWaitNS = max(metrics.pendingMaxWaitNS, wait)
            }
            let force = keyframeCadence.nextSubmission(forceRecovery: needsKeyFrame)
            if force { needsKeyFrame = false }
            return force
        }
        let context = Unmanaged.passRetained(FrameContext(pending.frame)).toOpaque()
        let properties = forceKey ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary : nil
        var flags = VTEncodeInfoFlags()
        // Retrying the final raw state after transport failure must not resubmit
        // an old timestamp to VideoToolbox. The wire retains captureTimeNS.
        var presentationTime = pending.frame.presentationTime
        if lastSubmittedPTS.isValid && CMTimeCompare(presentationTime, lastSubmittedPTS) <= 0 {
            presentationTime = CMTimeAdd(lastSubmittedPTS, CMTime(value: 1, timescale: options.fps))
        }
        lastSubmittedPTS = presentationTime
        submissionPacer.submitted(at: DispatchTime.now().uptimeNanoseconds)
        let result = VTCompressionSessionEncodeFrame(session, imageBuffer: pending.frame.image,
            presentationTimeStamp: presentationTime,
            duration: CMTime(value: 1, timescale: options.fps), frameProperties: properties,
            sourceFrameRefcon: context, infoFlagsOut: &flags)
        if result != noErr {
            Unmanaged<FrameContext>.fromOpaque(context).release()
            stateLock.withLock {
                metrics.encodeErrors += 1; needsKeyFrame = true
                reconnectAfterNS = DispatchTime.now().uptimeNanoseconds + 100_000_000
            }
            log("encode submission failed status=\(result)")
            finishFrame(retry: pending.frame)
        }
    }

    private func cancelScheduledDrain() {
        dispatchPrecondition(condition: .onQueue(captureQueue))
        scheduledDrain?.cancel()
        scheduledDrain = nil
        scheduledDrainTimer?.cancel()
        scheduledDrainTimer = nil
        drainWakeup.cancel()
    }

    private func scheduleDrain(at deadline: UInt64) {
        dispatchPrecondition(condition: .onQueue(captureQueue))
        guard let token = drainWakeup.schedule(at: deadline) else { return }
        scheduledDrain?.cancel()
        scheduledDrain = nil
        scheduledDrainTimer?.cancel()
        scheduledDrainTimer = nil
        if options.capEncoderFPS && options.strictPacerTimer {
            // One-shot only while a raw frame waits. No polling or spin loop.
            // Strict is best effort; the system still may deliver this late.
            let timer = DispatchSource.makeTimerSource(flags: .strict, queue: captureQueue)
            timer.schedule(deadline: DispatchTime(uptimeNanoseconds: deadline),
                           repeating: .never, leeway: .nanoseconds(0))
            timer.setEventHandler { [weak self] in self?.drainTimerFired(token: token, target: deadline) }
            scheduledDrainTimer = timer
            timer.resume()
        } else {
            let item = DispatchWorkItem { [weak self] in self?.drainTimerFired(token: token, target: deadline) }
            scheduledDrain = item
            captureQueue.asyncAfter(deadline: DispatchTime(uptimeNanoseconds: deadline), execute: item)
        }
    }

    private func drainTimerFired(token: UInt64, target: UInt64) {
        dispatchPrecondition(condition: .onQueue(captureQueue))
        guard !captureStopped, drainWakeup.consume(token) else { return }
        let fired = DispatchTime.now().uptimeNanoseconds
        stateLock.withLock { metrics.drainWake.record(target: target, fired: fired) }
        scheduledDrain = nil
        scheduledDrainTimer?.cancel()
        scheduledDrainTimer = nil
        drainPendingFrame()
    }

    // Complete only after the encoded AU has been fully written. Scheduling on
    // the capture queue serializes this drain with incoming SCK callbacks.
    private func finishFrame(retry: OwnedRawFrame? = nil) {
        captureQueue.async { [self] in
            // Invalidate any old rate/retry callback before completing or retrying
            // the current frame; a late canceled callback cannot drain twice.
            cancelScheduledDrain()
            guard !captureStopped else { return }
            gate.complete()
            if let retry, !gate.hasPending {
                gate.offer(retry, at: retry.captureTimeNS,
                           blockedUntil: submissionPacer.earliestSubmission(retryAfter: stateLock.withLock { reconnectAfterNS }))
            }
            drainPendingFrame()
        }
    }

    private func encoded(frame: FrameContext, status: OSStatus, flags: VTEncodeInfoFlags, sample: CMSampleBuffer?) {
        guard status == noErr, !flags.contains(.frameDropped), let sample, CMSampleBufferDataIsReady(sample) else {
            stateLock.withLock {
                metrics.encodeErrors += 1; needsKeyFrame = true
                reconnectAfterNS = DispatchTime.now().uptimeNanoseconds + 100_000_000
            }
            finishFrame(retry: frame.raw)
            log("encode callback failed status=\(status) dropped=\(flags.contains(.frameDropped))")
            return
        }
        let encodeTime = DispatchTime.now().uptimeNanoseconds - frame.submittedNS
        do {
            let (payload, keyframe) = try annexB(sample)
            stateLock.lock(); metrics.encoded += 1; metrics.encodeTimeNS += encodeTime; stateLock.unlock()
            outputQueue.async { [self] in
                var packet = Data("H264".utf8)
                appendBE(UInt16(options.refineTiles ? 2 : 1), to: &packet)
                appendBE(UInt16(keyframe ? 1 : 0), to: &packet)
                appendBE(UInt32(payload.count), to: &packet)
                appendBE(sequence, to: &packet)
                appendBE(frame.captureTimeNS, to: &packet)
                appendBE(UInt32(0), to: &packet)
                if options.refineTiles {
                    // Damage accumulates across captures omitted by the raw-frame gate.
                    let mask = TileGrid.changedMask(frame.tileVersions ?? [], since: lastSentTileVersions)
                    appendBE(mask, to: &packet)
                }
                packet.append(payload)
                do {
                    let result = try transport.write(packet)
                    refinement?.frameSent(sequence: sequence)
                    if let versions = frame.tileVersions {
                        tileRefinement?.frameSent(versions: versions, sequence: sequence,
                                                  connectionGeneration: transport.connectionGeneration)
                        lastSentTileVersions = versions
                    }
                    sequence &+= 1
                    stateLock.lock()
                    metrics.written += 1; metrics.bytes += packet.count; metrics.writeTimeNS += result.durationNS
                    if result.blocked { metrics.blockedWrites += 1 }
                    stateLock.unlock()
                    frame.raw = nil
                    finishFrame()
                } catch {
                    lastSentTileVersions = nil
                    stateLock.lock()
                    metrics.writeErrors += 1; needsKeyFrame = true
                    reconnectAfterNS = DispatchTime.now().uptimeNanoseconds + 1_000_000_000
                    stateLock.unlock()
                    log("transport reset reason=\(error); next frame will be IDR")
                    finishFrame(retry: frame.raw)
                }
            }
        } catch {
            stateLock.withLock {
                metrics.encodeErrors += 1; needsKeyFrame = true
                reconnectAfterNS = DispatchTime.now().uptimeNanoseconds + 100_000_000
            }
            finishFrame(retry: frame.raw)
            log("bitstream conversion failed \(error)")
        }
    }

    private func annexB(_ sample: CMSampleBuffer) throws -> (Data, Bool) {
        guard let format = CMSampleBufferGetFormatDescription(sample), let block = CMSampleBufferGetDataBuffer(sample) else {
            throw Failure("missing encoded format or buffer")
        }
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[CFString: Any]]
        let keyframe = (attachments?.first?[kCMSampleAttachmentKey_NotSync] as? Bool) != true
        let startCode = Data([0, 0, 0, 1])
        var result = Data()
        var parameterSetCount = 0, headerLength: Int32 = 0
        var parameterPointer: UnsafePointer<UInt8>?, parameterLength = 0
        let query = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format, parameterSetIndex: 0,
            parameterSetPointerOut: &parameterPointer, parameterSetSizeOut: &parameterLength,
            parameterSetCountOut: &parameterSetCount, nalUnitHeaderLengthOut: &headerLength)
        guard query == noErr, (1...4).contains(headerLength) else { throw Failure("invalid H264 format") }
        if keyframe {
            for index in 0..<parameterSetCount {
                let status = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format, parameterSetIndex: index,
                    parameterSetPointerOut: &parameterPointer, parameterSetSizeOut: &parameterLength,
                    parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil)
                guard status == noErr, let pointer = parameterPointer else { throw Failure("missing H264 parameter set") }
                result.append(startCode); result.append(pointer, count: parameterLength)
            }
        }
        let length = CMBlockBufferGetDataLength(block)
        guard length > 0, length <= 8 * 1024 * 1024 else { throw Failure("invalid access unit size") }
        var raw = Data(count: length)
        let copyStatus = raw.withUnsafeMutableBytes {
            CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!)
        }
        guard copyStatus == noErr else { throw Failure("encoded buffer copy failed") }
        var offset = 0
        while offset < raw.count {
            guard offset + Int(headerLength) <= raw.count else { throw Failure("truncated NAL length") }
            var nalLength = 0
            for index in 0..<Int(headerLength) { nalLength = (nalLength << 8) | Int(raw[offset + index]) }
            offset += Int(headerLength)
            guard nalLength > 0, nalLength <= raw.count - offset else { throw Failure("invalid NAL payload length") }
            result.append(startCode); result.append(raw.subdata(in: offset..<(offset + nalLength)))
            offset += nalLength
        }
        // An AUD boundary lets a raw Annex-B parser release the current access
        // unit without waiting for the next screen change (capture is variable-rate).
        result.append(contentsOf: [0, 0, 0, 1, 9, 0xF0])
        return (result, keyframe)
    }

    private func appendBE<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
        var big = value.bigEndian
        withUnsafeBytes(of: &big) { data.append(contentsOf: $0) }
    }

    private func reportMetrics() {
        let now = DispatchTime.now().uptimeNanoseconds
        stateLock.lock()
        let values = metrics; metrics = Metrics()
        let busy = gate.inFlight
        stateLock.unlock()
        let elapsed = Double(now - lastMetricsNS) / 1e9
        lastMetricsNS = now
        let encodeMS = values.encoded > 0 ? Double(values.encodeTimeNS) / Double(values.encoded) / 1e6 : 0
        let writeMS = values.written > 0 ? Double(values.writeTimeNS) / Double(values.written) / 1e6 : 0
        let copyMS = values.copied > 0 ? Double(values.copyTimeNS) / Double(values.copied) / 1e6 : 0
        let pendingMS = values.pendingDrained > 0 ? Double(values.pendingWaitNS) / Double(values.pendingDrained) / 1e6 : 0
        let captureLagMS = values.displayTimeFrames > 0 ? values.captureLagSumNS / Double(values.displayTimeFrames) / 1e6 : 0
        let captureLags = values.captureLagSamplesNS.sorted()
        let captureP50MS: Double
        if captureLags.isEmpty {
            captureP50MS = 0
        } else {
            let middle = captureLags.count / 2
            captureP50MS = captureLags.count.isMultiple(of: 2)
                ? (Double(captureLags[middle - 1]) + Double(captureLags[middle])) / 2e6
                : Double(captureLags[middle]) / 1e6
        }
        let displayTimeSources = values.displayTimeReasons.keys.sorted().map {
            "\($0):\(values.displayTimeReasons[$0]!)"
        }.joined(separator: ",")
        let displaySteps = values.displayStepSamplesNS.sorted()
        let stepMeanMS = values.displayStepCount > 0 ? values.displayStepSumNS / Double(values.displayStepCount) / 1e6 : 0
        let stepP50MS = displaySteps.isEmpty ? 0 : Double(displaySteps[displaySteps.count / 2]) / 1e6
        let statusNames = [0: "complete", 1: "idle", 2: "blank", 3: "suspended", 4: "started", 5: "stopped"]
        let statuses = values.captureStatuses.keys.sorted().map {
            "\(statusNames[$0] ?? "unknown"):\(values.captureStatuses[$0]!)"
        }.joined(separator: ",")
        log(String(format: "metrics interval=%.2fs callbacks=%d complete=%d encoded=%d sent=%d skipped=%d fps=%.1f Mbps=%.2f encode_ms=%.2f copy_ms=%.2f write_ms=%.2f writeblocked=%d encode_errors=%d copy_errors=%d write_errors=%d pending=%@ pending_raw=%@ pending_replaced=%d pending_drained=%d pending_wait_ms=%.2f pending_max_wait_ms=%.2f zero_copy_frames=%d statuses=%@ capture_time_source=callback display_time_sources=%@ display_time_valid=%d display_time_invalid=%d callback_minus_display_ms=%.2f callback_minus_display_p50_ms=%.2f callback_minus_display_min_ms=%.2f callback_minus_display_max_ms=%.2f display_time_samples=%d",
            elapsed, values.callbacks, values.complete, values.encoded, values.written, values.skipped,
            Double(values.written) / elapsed, Double(values.bytes) * 8 / elapsed / 1e6,
            encodeMS, copyMS, writeMS, values.blockedWrites, values.encodeErrors, values.copyErrors, values.writeErrors,
            busy ? "true" : "false", gate.hasPending ? "true" : "false", values.pendingReplaced, values.pendingDrained,
            pendingMS, Double(values.pendingMaxWaitNS) / 1e6, values.zeroCopyFrames, statuses,
            displayTimeSources, values.displayTimeFrames, values.callbackTimeFrames,
            captureLagMS, captureP50MS, values.captureLagMinNS / 1e6, values.captureLagMaxNS / 1e6, captureLags.count)
            + String(format: " display_step_count=%d display_step_mean_ms=%.2f display_step_p50_ms=%.2f display_step_max_ms=%.2f display_step_repeat=%d display_step_reverse=%d display_step_lt12ms=%d display_step_gt25ms=%d",
                values.displayStepCount, stepMeanMS, stepP50MS, Double(values.displayStepMaxNS) / 1e6,
                values.displayStepRepeat, values.displayStepReverse, values.displayStepBelow12MS, values.displayStepAbove25MS)
            + String(format: " drain_wake_count=%d drain_wake_delta_mean_ms=%.3f drain_wake_delta_p50_ms=%.3f drain_wake_late_max_ms=%.3f drain_wake_early_count=%d drain_wake_sample_count=%d",
                values.drainWake.count, values.drainWake.meanDeltaNS / 1e6,
                values.drainWake.sampledMedianDeltaNS / 1e6, Double(values.drainWake.maximumLateNS) / 1e6,
                values.drainWake.earlyCount, values.drainWake.samplesNS.count))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard stateLock.withLock({ stream === self.stream }) else { return }
        captureQueue.async { [self] in
            captureStopped = true
            cancelScheduledDrain()
            timer?.cancel()
            log("capture stopped error=\(error)")
            Darwin.exit(3)
        }
    }
}

@main
private enum Main {
    static func main() {
        do {
            let options = try Options.parse()
            let app = NSApplication.shared
            app.setActivationPolicy(.accessory)
            let host = DisplayHost(options: options)
            Task {
                do { try await host.start() }
                catch { log("startup failed \(error)"); Darwin.exit(2) }
            }
            withExtendedLifetime(host) { app.run() }
        } catch {
            log("\(error)\nUsage: WirelessDisplayHost --display-id ID [--port 16010] [--fps 60] [--capture-fps 60|0|120] [--bitrate 6000000] [--keyframe-interval 30] [--cap-encoder-fps false] [--strict-pacer-timer false]")
            Darwin.exit(1)
        }
    }
}
