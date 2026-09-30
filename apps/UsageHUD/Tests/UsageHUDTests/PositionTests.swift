import AppKit
import SwiftUI
import XCTest
@testable import UsageHUD

final class PositionTests: XCTestCase {
    func testNotchAnchorCentersAtScreenTop() {
        let frame = CGRect(x: 1000, y: 200, width: 1512, height: 982)
        let origin = notchAnchoredOrigin(size: CGSize(width: 380, height: 900), screenFrame: frame)
        XCTAssertEqual(origin.x, 1566)
        XCTAssertEqual(origin.y, 282)
    }

    func testCollapsedContentOccupiesOnlyPhysicalNotch() {
        let savedSize = hudPanelSize
        defer { hudPanelSize = savedSize }
        hudPanelSize = CGSize(width: 640, height: 900)

        let rect = contentRect(
            isCollapsed: true,
            expandedHeight: 396,
            expandedWidth: minPanelWidth,
            notchWidth: 126,
            notchHeight: 38
        )

        XCTAssertEqual(rect, CGRect(x: 257, y: 862, width: 126, height: 38))
    }

    func testExpandedContentStartsAtScreenTopAndIncludesNotchHeight() {
        let savedSize = hudPanelSize
        defer { hudPanelSize = savedSize }
        hudPanelSize = CGSize(width: 640, height: 900)

        let rect = contentRect(
            isCollapsed: false,
            expandedHeight: 396,
            expandedWidth: minPanelWidth,
            notchWidth: 126,
            notchHeight: 38
        )

        XCTAssertEqual(rect, CGRect(x: 28, y: 466, width: 584, height: 434))
    }

    func testExpandedContentFollowsTheCardWidth() {
        let savedSize = hudPanelSize
        defer { hudPanelSize = savedSize }
        hudPanelSize = CGSize(width: 1000, height: 900)

        let rect = contentRect(
            isCollapsed: false,
            expandedHeight: 396,
            expandedWidth: 800,
            notchWidth: 126,
            notchHeight: 38
        )

        XCTAssertEqual(rect, CGRect(x: 100, y: 466, width: 800, height: 434))
    }

    func testPanelWidthClampsToTheScreen() {
        XCTAssertEqual(clampedPanelWidth(nil, screenWidth: 1512), 584)
        XCTAssertEqual(clampedPanelWidth(.nan, screenWidth: 1512), 584)
        XCTAssertEqual(clampedPanelWidth(500, screenWidth: 1512), 584)
        XCTAssertEqual(clampedPanelWidth(700, screenWidth: 1512), 700)
        XCTAssertEqual(clampedPanelWidth(5000, screenWidth: 1512), 960)
        XCTAssertEqual(clampedPanelWidth(900, screenWidth: 800), 720)
        // A screen too narrow for the maximum still keeps the minimum.
        XCTAssertEqual(clampedPanelWidth(900, screenWidth: 600), 584)
    }

    @MainActor
    func testSavedWidthSurvivesANewModelAndASmallerScreenDoesNotOverwriteIt() {
        let suite = "UsageHUDTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let first = UsageModel(defaults: defaults)
        first.applyScreenWidth(1512)
        XCTAssertEqual(first.panelWidth, 584)
        first.resizePanel(to: 760)
        first.savePanelWidth()

        let small = UsageModel(defaults: defaults)
        small.applyScreenWidth(800)
        XCTAssertEqual(small.panelWidth, 720)

        let wide = UsageModel(defaults: defaults)
        wide.applyScreenWidth(1512)
        XCTAssertEqual(wide.panelWidth, 760)
    }

    func testFullNotchHoverRectIncludesTopEdgeAndShoulderArea() {
        let physical = CGRect(x: 790, y: 1131, width: 220, height: 38)
        let hover = fullNotchHoverRect(physical)

        XCTAssertEqual(hover, CGRect(x: 757, y: 1126, width: 286, height: 44))
        XCTAssertTrue(hover.contains(CGPoint(x: physical.midX, y: physical.maxY)))
        XCTAssertTrue(hover.contains(CGPoint(x: physical.minX - 24, y: physical.midY)))
        XCTAssertTrue(hover.contains(CGPoint(x: physical.midX, y: physical.minY - 4)))
    }

    func testAbsoluteTopCursorRemainsInsideAfterExpansion() {
        let screenTop: CGFloat = 1169
        let cursor = CGPoint(x: 900, y: screenTop)
        let collapsed = fullNotchHoverRect(
            CGRect(x: 790, y: screenTop - 38, width: 220, height: 38)
        )
        let expanded = topEdgeInclusiveRect(
            CGRect(x: 600, y: 700, width: 600, height: screenTop - 700)
        )

        XCTAssertTrue(collapsed.contains(cursor))
        XCTAssertTrue(expanded.contains(cursor))
    }

    @MainActor
    func testHostingViewHitTestKeepsTopAnchoredControlsClickable() {
        let window = NSWindow(
            contentRect: CGRect(x: 100, y: 200, width: 640, height: 900),
            styleMask: .borderless,
            backing: .buffered,
            defer: false
        )
        let view = HitTestScopedHostingView(rootView: Color.black)
        view.activeRectInScreenProvider = {
            CGRect(x: 120, y: 700, width: 600, height: 400)
        }
        window.contentView = view

        XCTAssertNotNil(view.hitTest(CGPoint(x: 230, y: 850)))
        XCTAssertNil(view.hitTest(CGPoint(x: 230, y: 50)))
    }

}
