import Foundation

func pruneHistory(_ samples: [HistorySample], now: Date, maxAgeDays: Double = 7, maxCount: Int = 5000) -> [HistorySample] {
    let cutoff = now.timeIntervalSince1970 - maxAgeDays * 86400
    let fresh = samples.filter { $0.asOf >= cutoff }.sorted { $0.asOf < $1.asOf }
    return fresh.count > maxCount ? Array(fresh.suffix(maxCount)) : fresh
}

func samplesFromRows(_ rows: [MeterRow], now: Date) -> [HistorySample] {
    rows.compactMap { row in
        guard row.isLive, let window = row.window, let pct = row.pct else { return nil }
        return HistorySample(label: row.label, window: window, pct: pct, asOf: row.asOf ?? now.timeIntervalSince1970)
    }
}

private struct HistorySampleKey: Hashable {
    let label: String
    let window: String
    let asOf: Double
}

/// Drops later duplicates of the same (label, window, asOf) — a stale re-fetch resubmitting an
/// already-recorded sample must not flood history and evict genuinely new samples.
func dedupHistory(_ samples: [HistorySample]) -> [HistorySample] {
    var seen = Set<HistorySampleKey>()
    return samples.filter { seen.insert(HistorySampleKey(label: $0.label, window: $0.window, asOf: $0.asOf)).inserted }
}

/// Enforces a hard byte budget on top of `pruneHistory`'s count cap: repeatedly drops the oldest 10%
/// (at least 1) and re-encodes until the JSON fits, so a burst of many small-window samples can't
/// grow history.json unbounded between the (rare) 7-day/5000-count prunes.
func capToByteBudget(_ samples: [HistorySample], maxBytes: Int = 512 * 1024) -> [HistorySample] {
    var current = samples
    while let data = try? JSONEncoder().encode(current), data.count > maxBytes, !current.isEmpty {
        current = Array(current.dropFirst(max(1, current.count / 10)))
    }
    return current
}

/// Persists usage samples to `$USAGE_HUD_STATE_DIR|~/.local/state/usage-hud/history.json`, pruned to
/// 7 days on every append. File IO failures degrade to in-memory only — history is a nicety, never a
/// blocker for the HUD.
final class HistoryStore {
    private let fileURL: URL

    init(directory: URL? = nil) {
        let dir = directory ?? HistoryStore.defaultDirectory()
        self.fileURL = dir.appendingPathComponent("history.json")
    }

    private static func defaultDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment["USAGE_HUD_STATE_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return URL(fileURLWithPath: ("~/.local/state/usage-hud" as NSString).expandingTildeInPath)
    }

    func load() -> [HistorySample] {
        guard let data = try? Data(contentsOf: fileURL),
              let samples = try? JSONDecoder().decode([HistorySample].self, from: data) else { return [] }
        return samples
    }

    /// Append, dedup, prune, cap, persist; returns the final in-memory list the caller should keep.
    @discardableResult
    func append(_ samples: [HistorySample], now: Date) -> [HistorySample] {
        let merged = capToByteBudget(pruneHistory(dedupHistory(load() + samples), now: now))
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(merged) { try? data.write(to: fileURL, options: .atomic) }
        return merged
    }
}
