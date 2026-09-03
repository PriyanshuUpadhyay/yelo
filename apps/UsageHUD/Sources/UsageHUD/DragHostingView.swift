import AppKit
import SwiftUI

/// Hosts the SwiftUI content, scopes hit-testing to the visible surface, and owns the activeAlways
/// tracking area that covers the complete physical notch even while another app is active.
final class HitTestScopedHostingView<Content: View>: NSHostingView<Content> {
    var activeRectInScreenProvider: (() -> NSRect)?
    var trackingRectInScreenProvider: (() -> NSRect)?
    var onMouseLocationChanged: (() -> Void)?

    private var activeTrackingArea: NSTrackingArea?

    // The panel is a nonactivating, borderless, never-key window of an accessory app, so every click
    // is a "first mouse". Default NSView rejects first-mouse, which swallows the click before the
    // SwiftUI Button's press gesture can engage. Accepting first-mouse lets the click land on content.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let window, let activeRect = activeRectInScreenProvider?() else { return nil }
        // `point` is already in the full-bleed content view's window coordinates. Converting it
        // through flipped NSHostingView coordinates mirrors top-anchored controls to the bottom.
        let activeRectInWindow = window.convertFromScreen(activeRect)
        return activeRectInWindow.contains(point) ? super.hitTest(point) : nil
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        updateTrackingAreas()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let activeTrackingArea {
            removeTrackingArea(activeTrackingArea)
            self.activeTrackingArea = nil
        }
        guard let window,
              let screenRect = trackingRectInScreenProvider?(),
              !screenRect.isEmpty else { return }

        let viewRect = convert(window.convertFromScreen(screenRect), from: nil)
        let trackingArea = NSTrackingArea(
            rect: viewRect,
            options: [.mouseEnteredAndExited, .mouseMoved, .activeAlways, .enabledDuringMouseDrag],
            owner: self
        )
        addTrackingArea(trackingArea)
        activeTrackingArea = trackingArea
    }

    override func mouseEntered(with event: NSEvent) {
        onMouseLocationChanged?()
    }

    override func mouseMoved(with event: NSEvent) {
        onMouseLocationChanged?()
    }

    override func mouseExited(with event: NSEvent) {
        onMouseLocationChanged?()
    }
}
