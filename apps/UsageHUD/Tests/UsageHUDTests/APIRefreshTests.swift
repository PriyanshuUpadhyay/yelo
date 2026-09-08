import Foundation
import Testing
@testable import UsageHUD

@Suite(.serialized)
struct APIRefreshTests {
    @Test @MainActor
    func manualFetchRunsOnceAndReadsUpdatedCache() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let executable = directory.appendingPathComponent("yelo fixture")
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> "$0.calls"
        if [ "$2" = fetch ]; then
          printf 'account: ok\\n'
          printf ready > "$0.ready"
        elif [ -f "$0.ready" ]; then
          printf '%s' '[{"label":"cx","provider":"codex","window":"7d","pct":42,"reset":"1h","state":"ok"}]'
        else
          printf '[]'
        fi
        """.write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let model = UsageModel(environment: ["YELO_BIN": executable.path,
                                             "USAGE_HUD_FETCH_SCRIPT": "/must-not-run"],
                               historyStore: HistoryStore(directory: directory))
        model.refresh()
        try await settled(model)
        let calls = URL(fileURLWithPath: executable.path + ".calls")
        #expect(try String(contentsOf: calls) == "usage show --json\n")
        model.fetchFromAPI()
        model.fetchFromAPI()
        try await settled(model)
        #expect(model.rows.first?.pct == 42)
        #expect(model.apiFetchResult?.warning == false)
        #expect(try String(contentsOf: calls) == "usage show --json\nusage fetch\nusage show --json\n")
    }

    @Test(arguments: [
        ("a: ok\nb: ok\n", Int32(0), false),
        ("a: ok\nb: auth-stale\n", Int32(0), true),
        ("a: ok\nb: fetch-failed\n", Int32(0), true),
        ("a: fable-write-failed\n", Int32(0), true),
        ("a: fetch-failed\n", Int32(1), true),
        ("", Int32(0), true),
        ("a: ok\n", Int32(2), true),
    ])
    func reportsPartialFailure(output: String, status: Int32, warning: Bool) {
        #expect(apiFetchSummary(output, exitCode: status).warning == warning)
    }

    /// A row confirmed after the fetch outranks the fetch's verdict for that account, so a
    /// launcher that renewed the token clears the mark without another Refresh.
    @Test
    func laterSampleSupersedesAccountVerdict() {
        var result = apiFetchSummary("cl·a: auth-stale\ncl·b: ok\n", exitCode: 0)
        result.at = Date(timeIntervalSince1970: 1_000)
        func row(_ label: String, seenAt: Double) -> MeterRow {
            MeterRow(label: label, provider: "claude", window: "5h", pct: 1, reset: "1h", state: "ok",
                     reason: nil, asOf: seenAt, seenAt: seenAt, active: nil, source: "statusline", canFetch: true)
        }
        #expect(supersededFetchResult(result, rows: [row("cl·a", seenAt: 900)])?.statuses["cl·a"] == "auth-stale")
        #expect(supersededFetchResult(result, rows: [row("cl·b", seenAt: 1_100)])?.statuses["cl·a"] == "auth-stale")
        #expect(supersededFetchResult(result, rows: [row("cl·a", seenAt: 1_100)]) == nil)
    }

    /// The footer count and the rows must name the same accounts, so the summary carries the
    /// per-account word keyed by the label the rows already use.
    @Test
    func reportsPerAccountStatuses() {
        let result = apiFetchSummary("cl·a: ok\ncx·b: fetch-failed\n", exitCode: 0)
        #expect(result.statuses == ["cl·a": "ok", "cx·b": "fetch-failed"])
        #expect(result.warning)
        #expect(result.message == "Updated 1 of 2 accounts.")
    }

    @Test
    func missingExecutableReportsFailure() {
        #expect(runAPIFetch(executable: "/missing/yelo").warning)
    }

    @Test
    func interruptedFetchCannotReportPartialOutputAsSuccess() throws {
        let executable = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: executable) }
        try "#!/bin/sh\nprintf 'account: ok\\n'\nexec /bin/sleep 5\n"
            .write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let result = runAPIFetch(executable: executable.path, timeout: 0.1)
        #expect(result.warning)
        #expect(result.message.contains("did not finish"))
    }

    @MainActor
    private func settled(_ model: UsageModel) async throws {
        let deadline = Date().addingTimeInterval(5)
        while (model.isFetching || model.isRefreshing) && Date() < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(!model.isFetching && !model.isRefreshing)
    }
}
