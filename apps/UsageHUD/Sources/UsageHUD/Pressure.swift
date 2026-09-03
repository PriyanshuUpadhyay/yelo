import Foundation

/// Parse a dock-script reset string ("17m", "4h37m", "2d5h", "12h", "now") into hours-until-reset.
/// "now"/"" → 0. Returns nil if no d/h/m component is present so callers can fall back to no-trend.
func parseResetHours(_ s: String) -> Double? {
    let trimmed = s.trimmingCharacters(in: .whitespaces)
    if trimmed.isEmpty || trimmed == "now" { return 0 }
    var hours = 0.0
    var found = false
    var number = ""
    for ch in trimmed {
        if ch.isNumber {
            number.append(ch)
        } else if let value = Double(number) {
            switch ch {
            case "d": hours += value * 24; found = true
            case "h": hours += value; found = true
            case "m": hours += value / 60; found = true
            default: return nil
            }
            number = ""
        } else {
            return nil
        }
    }
    if !number.isEmpty { return nil }
    return found ? hours : nil
}

struct HistorySample: Codable, Equatable {
    let label: String
    let window: String
    let pct: Int
    let asOf: Double   // unix seconds
}

enum PressureClass: Equatable { case red, amber, green }

struct RowPressure {
    let label: String
    let window: String
    let pct: Int
    let active: Bool
    let burn: Double?          // %/hr in the current segment; nil = no trend (v1 display)
    let hoursToReset: Double?
    let projected: Double?     // pct + burn * hoursToReset
    let eta100: Double?        // (100 - pct) / burn
    let pressure: PressureClass
}

/// A pct drop greater than this from the previous sample means the usage window reset.
private let resetDropThreshold = 10
/// Minimum span a burn-rate lookback must cover to count as signal, not noise.
private let minBurnSpanMinutes: Double = 10
/// pct is integer-quantized: a lone ±1 tick between two samples is quantization noise, not a
/// trend.
private let minBurnDeltaPct = 2

/// Samples for ONE (label,window) row, oldest→newest, restricted to the current segment. A pct drop
/// greater than `resetDropThreshold` from the previous sample means the usage window reset, so older
/// samples belong to a spent window and must not feed trend math.
func currentSegment(_ samples: [HistorySample]) -> [HistorySample] {
    let sorted = samples.sorted { $0.asOf < $1.asOf }
    guard sorted.count > 1 else { return sorted }
    var segmentStart = 0
    for i in 1..<sorted.count where sorted[i - 1].pct - sorted[i].pct > resetDropThreshold {
        segmentStart = i
    }
    return Array(sorted[segmentStart...])
}

/// %/hr from the oldest sample within `lookbackHours` of the latest to the latest. Requires ≥2
/// samples spanning ≥`minBurnSpanMinutes` AND a pct move ≥`minBurnDeltaPct`; otherwise nil (not
/// enough signal for a trend).
func burnRate(_ segment: [HistorySample], lookbackHours: Double) -> Double? {
    guard segment.count >= 2, let latest = segment.last else { return nil }
    let cutoff = latest.asOf - lookbackHours * 3600
    guard let earliest = segment.first(where: { $0.asOf >= cutoff }) else { return nil }
    let spanSeconds = latest.asOf - earliest.asOf
    guard spanSeconds >= minBurnSpanMinutes * 60 else { return nil }
    guard abs(latest.pct - earliest.pct) >= minBurnDeltaPct else { return nil }
    return Double(latest.pct - earliest.pct) / (spanSeconds / 3600)
}

/// Weekly-window trend: whole-window average pace. A lookback trend extrapolated across a
/// multi-day horizon red-walls on any single active hour (real trace: 3→5 in an hour at 5% used
/// → eta100 "beats" a 4-day reset), so weekly risk uses pct / hours-elapsed instead. No trend
/// until 24h of the window has elapsed — early-window division explodes; callers fall back to
/// the no-trend severity bands.
func weeklyAveragePace(pct: Int, hoursToReset: Double) -> Double? {
    let elapsed = 168 - hoursToReset
    guard elapsed >= 24 else { return nil }
    return Double(pct) / elapsed
}

/// projected needs both burn and reset (computed even for non-positive burn); eta100 needs only a
/// positive burn (computed even with no reset). red: eta100 and reset both known and eta100 beats
/// reset. amber: projected known and ≥85%. green otherwise.
func classify(pct: Int, burn: Double?, hoursToReset: Double?) -> (projected: Double?, eta100: Double?, pressure: PressureClass) {
    let projected: Double? = burn.flatMap { b in hoursToReset.map { Double(pct) + b * $0 } }
    let eta100: Double? = burn.flatMap { $0 > 0 ? (100.0 - Double(pct)) / $0 : nil }
    let pressure: PressureClass
    if let eta100, let htr = hoursToReset, eta100 < htr {
        pressure = .red
    } else if let projected, projected >= 85 {
        pressure = .amber
    } else {
        pressure = .green
    }
    return (projected, eta100, pressure)
}

func rowPressures(rows: [MeterRow], history: [HistorySample]) -> [RowPressure] {
    rows.compactMap { row in
        guard row.isLive, let window = row.window, let pct = row.pct else { return nil }
        let htr = row.reset.flatMap(parseResetHours)
        let burn: Double?
        switch window {
        case "5h":
            let segment = currentSegment(history.filter { $0.label == row.label && $0.window == window })
            burn = burnRate(segment, lookbackHours: 1)
        case "7d", "fb":   // fb = Fable weekly, same 7d-class window
            burn = htr.flatMap { weeklyAveragePace(pct: pct, hoursToReset: $0) }
        default:
            burn = nil   // unknown window shape: no trend, not a silent 7d guess
        }
        let (projected, eta100, pressure) = classify(pct: pct, burn: burn, hoursToReset: htr)
        return RowPressure(label: row.label, window: window, pct: pct, active: row.active != false,
                           burn: burn, hoursToReset: htr, projected: projected, eta100: eta100, pressure: pressure)
    }
}

private func rank(_ p: PressureClass) -> Int { switch p { case .red: return 0; case .amber: return 1; case .green: return 2 } }

/// The row the collapsed bubble tracks: active rows only, worst pressure wins (red > amber > green),
/// tie-broken by higher projected then higher pct.
func bindingRow(_ pressures: [RowPressure]) -> RowPressure? {
    pressures.filter { $0.active }.min { a, b in
        if rank(a.pressure) != rank(b.pressure) { return rank(a.pressure) < rank(b.pressure) }
        if (a.projected ?? 0) != (b.projected ?? 0) { return (a.projected ?? 0) > (b.projected ?? 0) }
        return a.pct > b.pct
    }
}

func pressureAnnotation(burn: Double, eta100: Double, now: Date, formatter: DateFormatter) -> String {
    let eta = now.addingTimeInterval(eta100 * 3600)
    return "+\(Int(burn.rounded()))%/hr →100% ~\(formatter.string(from: eta))"
}

/// What the collapsed bubble tints from: with no trend yet (`burn == nil`) the pressure class is an
/// uninformative `.green`, so fall back to the raw pct severity band; once a trend exists, the
/// pressure class is the truth. nil binding → no tint.
enum BubbleBasis: Equatable { case severity(Int); case pressure(PressureClass) }

func bubbleBasis(_ binding: RowPressure?) -> BubbleBasis? {
    guard let binding else { return nil }
    return binding.burn == nil ? .severity(binding.pct) : .pressure(binding.pressure)
}
