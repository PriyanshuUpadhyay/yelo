import SwiftUI

/// The one-glance verdict behind the header dot and caption: the same reading the bars carry,
/// reduced to a single word for the whole account list.
enum Mood: Equatable, CaseIterable {
    case calm, warn, danger, asleep, thinking

    var caption: String {
        switch self {
        case .calm: return "All calm"
        case .warn: return "Running warm"
        case .danger: return "Ease off"
        case .asleep: return "No fresh usage"
        case .thinking: return "Fetching from API…"
        }
    }

    var dotColor: Color {
        switch self {
        case .calm: return calmColor
        case .warn: return warnColor
        case .danger: return dangerColor
        case .asleep: return textTertiary
        case .thinking: return codexColor
        }
    }
}

/// A raw pct carries no trend, so it maps through the same severity bands the bars use; a real
/// pressure class already IS the verdict.
extension BubbleBasis {
    var severity: PresentationSeverity {
        switch self {
        case .severity(let pct): return PresentationSeverity(pct: pct, pressure: nil)
        case .pressure(.red): return .danger
        case .pressure(.amber): return .warn
        case .pressure(.green): return .calm
        }
    }
}

/// Fetching outranks everything (it is the one state the person just caused); with no live row
/// there is nothing to have an opinion about, so the verdict is "no fresh usage", not "calm".
func usageMood(
    hasLoadedSnapshot: Bool,
    isFetching: Bool,
    rows: [MeterRow],
    pressures: [RowPressure]
) -> Mood {
    if isFetching { return .thinking }
    guard hasLoadedSnapshot, rows.contains(where: \.isLive) else { return .asleep }
    switch bubbleBasis(bindingRow(pressures))?.severity {
    case .danger: return .danger
    case .warn: return .warn
    case .calm, nil: return .calm
    }
}
