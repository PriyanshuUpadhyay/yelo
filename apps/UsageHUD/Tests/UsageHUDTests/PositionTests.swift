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
            notchWidth: 126,
            notchHeight: 38
        )

        // The expanded surface is a constant 644 wide (expandedSurfaceWidth), never the
        // panel width and never the notch width: x = (640 - 644) / 2 = -2 under this
        // test's own 640 override, and y = 900 - (38 + 396). The real panel is 684 wide,
        // which is what keeps the surface inside it on a live screen.
        XCTAssertEqual(rect, CGRect(x: -2, y: 466, width: 644, height: 434))
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

    func testFullscreenWindowBoundsMatchDisplayWithinWindowServerRounding() {
        let display = CGRect(x: 1512, y: 0, width: 1728, height: 1117)
        let roundedWindow = CGRect(x: 1512.5, y: -0.5, width: 1727, height: 1118)

        XCTAssertTrue(windowBoundsMatchDisplay(roundedWindow, displayBounds: display))
    }

    func testOrdinaryWindowBoundsDoNotMatchDisplay() {
        let display = CGRect(x: 0, y: 0, width: 1512, height: 982)
        let window = CGRect(x: 0, y: 25, width: 1512, height: 957)

        XCTAssertFalse(windowBoundsMatchDisplay(window, displayBounds: display))
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
