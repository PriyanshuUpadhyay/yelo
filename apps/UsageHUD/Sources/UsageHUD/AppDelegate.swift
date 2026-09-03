import AppKit
import CoreGraphics
import SwiftUI

/// Expanded-panel height ceiling for a given screen: the panel has NO scrolling, so rather than a
/// fixed constant that clips when more profile groups appear, content may grow to the screen's
/// visible height minus a margin. The floor keeps a sane panel on tiny screens (content past the
/// ceiling clips, as before).
func hudPanelHeight(screenHeight: CGFloat) -> CGFloat { max(400, screenHeight - 40) }

/// AppKit panel frame — the max headroom the window can ever occupy; the VISIBLE expanded surface
/// hugs its measured content height (`UsageModel.expandedContentHeight`). Width is fixed. HEIGHT is
/// re-derived from the PLACEMENT screen every time the panel is (re)positioned (AppDelegate's
/// placement sites are the only writers) — a launch-time constant would bake in the wrong screen:
/// `NSScreen.main` follows key-window focus, which an accessory app never owns, so the panel is
/// placed on the built-in notched screen. Initial value is just a pre-first-placement default.
var hudPanelSize = NSSize(width: 684, height: hudPanelHeight(screenHeight: NSScreen.screens.first?.visibleFrame.height ?? 800))
let fallbackNotchSize = NSSize(width: 224, height: 38)
let expandedSurfaceWidth: CGFloat = 644
let notchHoverHorizontalInset: CGFloat = 8
let notchHoverBottomInset: CGFloat = 5
let notchHoverTopInset: CGFloat = 1

/// The rect (window content-view-local coordinates, origin bottom-left) actually occupied by
/// visible content — the physical notch when collapsed, the content-fitted card when expanded — both
/// attached to the screen's top edge inside the fixed `hudPanelSize` window. Expanded width is
/// deterministic: content measurement can change while SwiftUI mounts the expanding hierarchy, so
/// using it to size the mask can transiently crop the content against the notch shoulders.
func contentRect(
    isCollapsed: Bool,
    expandedHeight: CGFloat,
    notchWidth: CGFloat,
    notchHeight: CGFloat
) -> NSRect {
    let size = isCollapsed
        ? NSSize(width: notchWidth, height: notchHeight)
        : NSSize(
            width: expandedSurfaceWidth,
            height: notchHeight + expandedHeight
        )
    return NSRect(
        x: (hudPanelSize.width - size.width) / 2,
        y: hudPanelSize.height - size.height,
        width: size.width,
        height: size.height
    )
}

func physicalNotchRect(
    panelFrame: NSRect,
    notchWidth: CGFloat,
    notchHeight: CGFloat
) -> NSRect {
    NSRect(
        x: panelFrame.midX - notchWidth / 2,
        y: panelFrame.maxY - notchHeight,
        width: notchWidth,
        height: notchHeight
    )
}

func fullNotchHoverRect(_ physicalNotch: NSRect) -> NSRect {
    let sideWidth = max(0, physicalNotch.height - 12) + 24
    return NSRect(
        x: physicalNotch.midX - (physicalNotch.width + sideWidth) / 2 - notchHoverHorizontalInset,
        y: physicalNotch.minY - notchHoverBottomInset,
        width: physicalNotch.width + sideWidth + notchHoverHorizontalInset * 2,
        height: physicalNotch.height + notchHoverBottomInset + notchHoverTopInset
    )
}

func topEdgeInclusiveRect(_ rect: NSRect) -> NSRect {
    NSRect(
        x: rect.minX,
        y: rect.minY,
        width: rect.width,
        height: rect.height + notchHoverTopInset
    )
}

func windowBoundsMatchDisplay(
    _ windowBounds: CGRect,
    displayBounds: CGRect,
    tolerance: CGFloat = 1
) -> Bool {
    abs(windowBounds.minX - displayBounds.minX) <= tolerance
        && abs(windowBounds.minY - displayBounds.minY) <= tolerance
        && abs(windowBounds.maxX - displayBounds.maxX) <= tolerance
        && abs(windowBounds.maxY - displayBounds.maxY) <= tolerance
}

private let hudLogFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

/// One timestamped line to stderr (lands in usage-hud.err.log). Shared by visibility-decision
/// logging (AppDelegate) and the on-demand API fetch (UsageModel) so both use one channel/format.
func hudLog(_ message: String) {
    let line = "\(hudLogFormatter.string(from: Date())) \(message)\n"
    FileHandle.standardError.write(Data(line.utf8))
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let model = UsageModel()
    private var statusItem: NSStatusItem!
    private var panel: NSPanel!
    private var hostingView: HitTestScopedHostingView<ContentView>!
    private var showHideItem: NSMenuItem!

    // The panel's WindowServer window must stay tagged onto every Space; the exact set is asserted at
    // build, re-asserted on unlock, and round-tripped through [] during wedge recovery (see below).
    private let hudCollectionBehavior: NSWindow.CollectionBehavior = [
        .canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle,
    ]
    private let expandDelay: TimeInterval = 0.3
    private let collapseDelay: TimeInterval = 0.5
    // Delay between an explicit show (orderFront) and the CGWindowList probe that confirms the panel
    // actually reached the active Space — enough for WindowServer to settle the order-in.
    private let visCheckDelay: TimeInterval = 1.0
    private let maxWedgeRecoveryAttempts = 3

    private var pendingCollapse: DispatchWorkItem?
    private var pendingExpand: DispatchWorkItem?
    private var pendingVisCheck: DispatchWorkItem?
    // Recovery attempts spent in the current wedge episode; reset to 0 on any confirmed on-screen
    // verification (fresh show or successful recovery) so a healthy panel always starts clean.
    private var wedgeRecoveryAttempts = 0
    // Bumped whenever an episode is abandoned (a hide). A recovery follow-up captures the value at
    // schedule time and no-ops if it changed, so an interrupted episode's stale continuation can't
    // count an attempt against — or otherwise disturb — a later one.
    private var wedgeGeneration = 0
    private var globalMouseMonitor: Any?
    private var localMouseMonitor: Any?
    private var clickThroughState: Bool?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        buildStatusItem()
        buildPanel()
        repositionToNotch()
        model.start()

        NotificationCenter.default.addObserver(
            self, selector: #selector(screenParametersChanged), name: NSApplication.didChangeScreenParametersNotification, object: nil
        )
        // Prevention: after an overnight lock the panel's window can drop its all-spaces tag; re-assert
        // the collection behavior on unlock without changing visibility.
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(screenUnlocked), name: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil
        )
        showPanel(refresh: false, trigger: "launch")
    }

    // MARK: Visibility-decision logging
    //
    // Every show/hide transition prints one line to stderr (lands in usage-hud.err.log).

    /// One stderr line for a visibility decision. `onScreen` is included only when the caller already
    /// has the CGWindowList answer cheaply at hand (nil = not probed, to avoid an extra scan per line).
    private func logVisibility(_ decision: String, trigger: String, onScreen: Bool? = nil) {
        let onScreenStr = onScreen.map { " onScreen=\($0)" } ?? ""
        hudLog("[vis] \(decision) trigger=\(trigger) visible=\(panel.isVisible)\(onScreenStr)")
    }

    // MARK: Status item / menu

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(
            systemSymbolName: "gauge.with.needle", accessibilityDescription: "Usage HUD"
        )

        let menu = NSMenu()
        showHideItem = NSMenuItem(title: "Hide HUD", action: #selector(toggleShowHide), keyEquivalent: "")
        showHideItem.target = self
        menu.addItem(showHideItem)

        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refreshNow), keyEquivalent: "")
        refreshItem.target = self
        menu.addItem(refreshItem)

        let fetchItem = NSMenuItem(title: "Fetch usage from API", action: #selector(fetchFromAPI), keyEquivalent: "")
        fetchItem.target = self
        menu.addItem(fetchItem)

        menu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        menu.delegate = self
        statusItem.menu = menu
    }

    @objc private func toggleShowHide() {
        if panel.isVisible {
            hidePanel(trigger: "menu")
        } else {
            showPanel(refresh: true, trigger: "menu")
        }
    }

    private func showPanel(refresh: Bool, trigger: String) {
        logVisibility("SHOW", trigger: trigger)
        repositionToNotch()
        panel.orderFrontRegardless()
        startMouseMonitoring()
        if refresh { model.refresh() }
        handleMouseLocationChanged()
        scheduleVisibilityCheck()
    }

    private func hidePanel(trigger: String) {
        logVisibility("HIDE", trigger: trigger)
        // A hide abandons any wedge episode: cancel the pending post-show verification, reset the
        // attempt counter, and bump the generation so an already-scheduled recovery follow-up no-ops.
        pendingVisCheck?.cancel()
        pendingVisCheck = nil
        wedgeRecoveryAttempts = 0
        wedgeGeneration += 1
        panel.orderOut(nil)
        stopMouseMonitoring()
        // A manual hide always resets to the notch silhouette.
        pendingCollapse?.cancel()
        pendingCollapse = nil
        pendingExpand?.cancel()
        pendingExpand = nil
        collapse()
    }

    @objc private func refreshNow() {
        model.refresh()
    }

    @objc private func fetchFromAPI() {
        model.fetchFromAPI()
    }

    @objc private func screenParametersChanged(_ note: Notification) {
        repositionToNotch()
        hostingView.updateTrackingAreas()
        handleMouseLocationChanged()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    @objc private func screenUnlocked() {
        reassertCollectionBehavior()
    }

    // MARK: Panel

    private func buildPanel() {
        panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: hudPanelSize),
            styleMask: [.nonactivatingPanel, .borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .mainMenu + 3
        panel.collectionBehavior = hudCollectionBehavior
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.acceptsMouseMovedEvents = true
        clickThroughState = nil

        let view = HitTestScopedHostingView(rootView: ContentView(model: model))
        view.activeRectInScreenProvider = { [weak self] in
            guard let self else { return .zero }
            return self.contentRectInScreen(isCollapsed: self.model.isCollapsed)
        }
        view.trackingRectInScreenProvider = { [weak self] in
            guard let self else { return .zero }
            return self.model.isCollapsed
                ? self.collapsedHoverRectInScreen()
                : self.expandedHoverRectInScreen()
        }
        view.onMouseLocationChanged = { [weak self] in
            self?.handleMouseLocationChanged()
        }
        hostingView = view
        panel.contentView = view
    }

    // The built-in notched display is the stable home; notchless Macs fall back to the cursor screen.
    private func screenUnderCursor() -> NSScreen {
        let mouseLocation = NSEvent.mouseLocation
        return NSScreen.screens.first { $0.frame.contains(mouseLocation) } ?? NSScreen.main ?? NSScreen.screens[0]
    }

    private func notchScreen() -> NSScreen {
        NSScreen.screens.first {
            guard let id = $0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID else {
                return false
            }
            return CGDisplayIsBuiltin(id) != 0 && $0.safeAreaInsets.top > 0
        } ?? screenUnderCursor()
    }

    /// The ceiling is a function of the screen the panel lands on — every placement site calls this
    /// with its chosen screen before using `hudPanelSize`.
    private func updatePanelCeiling(for screen: NSScreen) {
        model.applyPanelCeiling(hudPanelHeight(screenHeight: screen.visibleFrame.height))
        let menuBarHeight = screen.frame.maxY - screen.visibleFrame.maxY
        model.notchTopInset = max(screen.safeAreaInsets.top, menuBarHeight)
        if screen.safeAreaInsets.top > 0 {
            let left = screen.auxiliaryTopLeftArea?.width ?? 0
            let right = screen.auxiliaryTopRightArea?.width ?? 0
            model.notchTriggerWidth = screen.frame.width - left - right
        } else {
            model.notchTriggerWidth = fallbackNotchSize.width
        }
    }

    private func repositionToNotch() {
        let screen = notchScreen()
        updatePanelCeiling(for: screen)
        let origin = notchAnchoredOrigin(size: hudPanelSize, screenFrame: screen.frame)
        panel.setFrame(NSRect(origin: origin, size: hudPanelSize), display: false)
    }

    // MARK: Hover expand/collapse — geometry-driven, single owner

    private func contentRectInScreen(isCollapsed: Bool) -> NSRect {
        return contentRect(
            isCollapsed: isCollapsed,
            expandedHeight: model.expandedContentHeight,
            notchWidth: model.notchTriggerWidth,
            notchHeight: model.notchTopInset
        )
            .offsetBy(dx: panel.frame.origin.x, dy: panel.frame.origin.y)
    }

    private func collapsedHoverRectInScreen() -> NSRect {
        fullNotchHoverRect(
            physicalNotchRect(
                panelFrame: panel.frame,
                notchWidth: model.notchTriggerWidth,
                notchHeight: model.notchTopInset
            )
        )
    }

    private func expandedHoverRectInScreen() -> NSRect {
        topEdgeInclusiveRect(contentRectInScreen(isCollapsed: false))
    }

    /// The ONE place hover state and click-through are decided, from raw screen-coordinate mouse
    /// location. The hosting view's activeAlways tracking area covers the full notch, and global plus
    /// local monitors provide cross-app fallbacks; every source feeds this single state transition.
    private func handleMouseLocationChanged() {
        guard panel.isVisible else { return }
        let isCollapsed = model.isCollapsed
        let mouseLocation = NSEvent.mouseLocation
        let hoverRect = isCollapsed ? collapsedHoverRectInScreen() : expandedHoverRectInScreen()
        let inside = hoverRect.contains(mouseLocation)
        setClickThrough(!inside)

        if isCollapsed {
            if inside {
                scheduleExpand()
            } else {
                pendingExpand?.cancel()
                pendingExpand = nil
            }
        } else if inside {
            pendingCollapse?.cancel()
            pendingCollapse = nil
        } else if pendingCollapse == nil {
            dismissFullscreenMenuBarIfNeeded()
            scheduleCollapse()
        }
    }

    private func scheduleExpand() {
        guard pendingExpand == nil else { return }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingExpand = nil
            guard self.model.isCollapsed,
                  self.collapsedHoverRectInScreen().contains(NSEvent.mouseLocation) else { return }
            self.expand()
        }
        pendingExpand = item
        DispatchQueue.main.asyncAfter(deadline: .now() + expandDelay, execute: item)
    }

    private func scheduleCollapse() {
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingCollapse = nil
            guard !self.expandedHoverRectInScreen().contains(NSEvent.mouseLocation) else { return }
            self.collapse()
        }
        pendingCollapse = item
        DispatchQueue.main.asyncAfter(deadline: .now() + collapseDelay, execute: item)
    }

    private func expand() {
        guard model.isCollapsed else { return }
        pendingExpand?.cancel()
        pendingExpand = nil
        model.isCollapsed = false
        hostingView.updateTrackingAreas()
        handleMouseLocationChanged()
        model.refresh()   // fresh data every time it opens
    }

    private func collapse() {
        guard !model.isCollapsed else { return }
        pendingExpand?.cancel()
        pendingExpand = nil
        model.isCollapsed = true
        hostingView.updateTrackingAreas()
        handleMouseLocationChanged()
        dismissFullscreenMenuBarIfNeeded()
    }

    // MARK: Mouse monitoring

    // The activeAlways tracking area mirrors Notchi's complete-notch path. A global monitor remains
    // as a cross-app fallback for transitions where another process owns the current mouse event.
    private func startMouseMonitoring() {
        guard globalMouseMonitor == nil else { return }
        globalMouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved]) { [weak self] _ in
            self?.handleMouseLocationChanged()
        }
        localMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved]) { [weak self] event in
            self?.handleMouseLocationChanged()
            return event
        }
    }

    private func stopMouseMonitoring() {
        if let monitor = globalMouseMonitor { NSEvent.removeMonitor(monitor); globalMouseMonitor = nil }
        if let monitor = localMouseMonitor { NSEvent.removeMonitor(monitor); localMouseMonitor = nil }
    }

    private func setClickThrough(_ ignore: Bool) {
        guard clickThroughState != ignore else { return }
        clickThroughState = ignore
        panel.ignoresMouseEvents = ignore
    }

    private func dismissFullscreenMenuBarIfNeeded() {
        let screen = notchScreen()
        guard NSMenu.menuBarVisible(), frontmostAppHasFullscreenWindow(on: screen) else { return }
        NSMenu.setMenuBarVisible(false)
    }

    private func frontmostAppHasFullscreenWindow(on screen: NSScreen) -> Bool {
        guard let frontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier,
              let screenNumber = screen.deviceDescription[
                  NSDeviceDescriptionKey("NSScreenNumber")
              ] as? NSNumber else { return false }

        let displayBounds = CGDisplayBounds(CGDirectDisplayID(screenNumber.uint32Value))
        guard let windows = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: AnyObject]] else { return false }

        return windows.contains { info in
            guard (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == frontmostPID,
                  (info[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let boundsDictionary = info[kCGWindowBounds as String] as? [String: Any],
                  let bounds = CGRect(
                      dictionaryRepresentation: boundsDictionary as CFDictionary
                  ) else { return false }
            return windowBoundsMatchDisplay(bounds, displayBounds: displayBounds)
        }
    }

    // MARK: Post-show WindowServer wedge self-heal
    //
    // After an overnight screen lock the panel's WindowServer window can lose its canJoinAllSpaces
    // tag — bound to a stale Space while the active Space differs — so app-side `panel.isVisible`
    // stays true and orderFront fires, but the window never lands on the active Space
    // (kCGWindowIsOnscreen=false). External processes can't move another app's window across Spaces,
    // so recovery is in-app: re-tag by re-asserting collectionBehavior across an orderOut/orderFront
    // cycle, and if that still fails, recreate the panel outright.

    /// kCGWindowIsOnscreen for our OWN panel window — the only probe of whether the window actually
    /// reached the active Space, independent of app-side `panel.isVisible`.
    private func panelIsOnScreen() -> Bool {
        guard panel.windowNumber > 0,
              let list = CGWindowListCopyWindowInfo([.optionIncludingWindow], CGWindowID(panel.windowNumber)) as? [[String: AnyObject]],
              let info = list.first else { return false }
        return (info[kCGWindowIsOnscreen as String] as? Bool) ?? false
    }

    /// Round-trip through [] to force WindowServer to re-apply the all-spaces tag — assigning the same
    /// value the window already holds can be a no-op that doesn't re-tag.
    private func reassertCollectionBehavior() {
        panel.collectionBehavior = []
        panel.collectionBehavior = hudCollectionBehavior
    }

    /// Armed after every explicit show (never per-mouse-move); cancelled/re-armed by the next show/hide.
    private func scheduleVisibilityCheck() {
        pendingVisCheck?.cancel()
        let item = DispatchWorkItem { [weak self] in
            self?.pendingVisCheck = nil
            self?.verifyOnScreen()
        }
        pendingVisCheck = item
        DispatchQueue.main.asyncAfter(deadline: .now() + visCheckDelay, execute: item)
    }

    /// Only a real wedge — panel visible app-side, yet offscreen in WindowServer — triggers recovery.
    private func verifyOnScreen() {
        guard panel.isVisible else { return }
        if panelIsOnScreen() {
            wedgeRecoveryAttempts = 0
            return
        }
        hudLog("[vis] WEDGE detected onScreen=false")
        recoverFromWedge(stage: .retag)
    }

    private enum WedgeStage { case retag, recreate }

    private func recoverFromWedge(stage: WedgeStage) {
        guard wedgeRecoveryAttempts < maxWedgeRecoveryAttempts else {
            hudLog("[vis] WEDGE recovery exhausted attempts=\(wedgeRecoveryAttempts)")
            return
        }
        wedgeRecoveryAttempts += 1
        switch stage {
        case .retag:
            panel.orderOut(nil)
            reassertCollectionBehavior()
            panel.orderFrontRegardless()
        case .recreate:
            recreatePanel()
        }
        let label = stage == .retag ? "retag" : "recreate"
        let gen = wedgeGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + visCheckDelay) { [weak self] in
            guard let self, self.wedgeGeneration == gen, self.panel.isVisible else { return }
            if self.panelIsOnScreen() {
                self.wedgeRecoveryAttempts = 0
                hudLog("[vis] WEDGE recovered=\(label)")
                return
            }
            // retag didn't take → escalate to a full recreate; a failed recreate re-enters retag for
            // another bounded round (capped by maxWedgeRecoveryAttempts).
            self.recoverFromWedge(stage: stage == .retag ? .recreate : .retag)
        }
    }

    /// Last-resort recovery: rebuild the panel from scratch (fresh WindowServer window) and restore
    /// its frame. `buildPanel` re-attaches the hosting/content view and re-wires every callback.
    private func recreatePanel() {
        let frame = panel.frame
        panel.orderOut(nil)
        buildPanel()
        panel.setFrame(frame, display: false)
        panel.orderFrontRegardless()
        hostingView.updateTrackingAreas()
        handleMouseLocationChanged()
    }

}

extension AppDelegate: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        showHideItem.title = panel.isVisible ? "Hide HUD" : "Show HUD"
    }
}
