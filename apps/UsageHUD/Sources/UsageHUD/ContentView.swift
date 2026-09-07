import AppKit
import SwiftUI

let warnColor = Color(red: 0xF5 / 255, green: 0x93 / 255, blue: 0x11 / 255)
let dangerColor = Color(red: 0xE5 / 255, green: 0x48 / 255, blue: 0x4D / 255)
let warnTextColor = warnColor
let dangerTextColor = dangerColor
let claudeColor = Color(red: 0xD9 / 255, green: 0x77 / 255, blue: 0x57 / 255)
let codexColor = Color(red: 0x2D / 255, green: 0x7F / 255, blue: 0xF9 / 255)
let microFade = 0.14

// Bars run base → light so the fill reads as lit rather than painted.
let claudeLightColor = Color(red: 0xF2 / 255, green: 0xA2 / 255, blue: 0x7E / 255)
let codexLightColor = Color(red: 0x6F / 255, green: 0xB2 / 255, blue: 0xFF / 255)
let warnLightColor = Color(red: 0xFF / 255, green: 0xC1 / 255, blue: 0x5E / 255)
let dangerLightColor = Color(red: 0xFF / 255, green: 0x7B / 255, blue: 0x7F / 255)
let calmColor = Color(red: 0x5A / 255, green: 0xD8 / 255, blue: 0xA4 / 255)

// The panel is always dark, so the surface is stated outright instead of inherited.
let surfaceTopColor = Color(red: 0x11 / 255, green: 0x11 / 255, blue: 0x16 / 255)
let surfaceBottomColor = Color(red: 0x09 / 255, green: 0x09 / 255, blue: 0x0B / 255)
let cardInsetColor = Color.white.opacity(0.04)
let hairlineColor = Color.white.opacity(0.07)
let edgeStrokeColor = Color.white.opacity(0.06)
let textPrimary = Color.white.opacity(0.92)
let textSecondary = Color.white.opacity(0.55)
let textTertiary = Color.white.opacity(0.35)

enum PresentationSeverity {
    case calm, warn, danger

    init(pct: Int, pressure: PressureClass?) {
        if pct >= 90 || pressure == .red { self = .danger }
        else if pct >= 70 || pressure == .amber { self = .warn }
        else { self = .calm }
    }

    var barColor: Color? {
        switch self {
        case .danger: return dangerColor
        case .warn: return warnColor
        case .calm: return nil
        }
    }

    var textColor: Color? {
        switch self {
        case .danger: return dangerTextColor
        case .warn: return warnTextColor
        case .calm: return nil
        }
    }

}

private struct NotchBackdropShape: Shape {
    var topShoulder: CGFloat
    var bottomRadius: CGFloat

    var animatableData: AnimatablePair<CGFloat, CGFloat> {
        get { AnimatablePair(topShoulder, bottomRadius) }
        set {
            topShoulder = newValue.first
            bottomRadius = newValue.second
        }
    }

    func path(in rect: CGRect) -> Path {
        let shoulder = min(topShoulder, rect.height / 3)
        let radius = min(bottomRadius, rect.height / 2)
        var path = Path()

        path.move(to: CGPoint(x: rect.minX, y: rect.minY))
        path.addQuadCurve(
            to: CGPoint(x: rect.minX + shoulder, y: rect.minY + shoulder),
            control: CGPoint(x: rect.minX + shoulder, y: rect.minY)
        )
        path.addLine(to: CGPoint(x: rect.minX + shoulder, y: rect.maxY - radius))
        path.addQuadCurve(
            to: CGPoint(x: rect.minX + shoulder + radius, y: rect.maxY),
            control: CGPoint(x: rect.minX + shoulder, y: rect.maxY)
        )
        path.addLine(to: CGPoint(x: rect.maxX - shoulder - radius, y: rect.maxY))
        path.addQuadCurve(
            to: CGPoint(x: rect.maxX - shoulder, y: rect.maxY - radius),
            control: CGPoint(x: rect.maxX - shoulder, y: rect.maxY)
        )
        path.addLine(to: CGPoint(x: rect.maxX - shoulder, y: rect.minY + shoulder))
        path.addQuadCurve(
            to: CGPoint(x: rect.maxX, y: rect.minY),
            control: CGPoint(x: rect.maxX - shoulder, y: rect.minY)
        )
        path.closeSubpath()
        return path
    }
}

struct ContentView: View {
    @ObservedObject var model: UsageModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    private var isCollapsed: Bool { model.isCollapsed }

    private var currentSize: NSSize {
        if isCollapsed {
            return NSSize(width: model.notchTriggerWidth, height: model.notchTopInset)
        }
        return NSSize(
            width: expandedSurfaceWidth,
            height: model.notchTopInset + model.expandedContentHeight
        )
    }

    /// Opening is the eye-catching move and can overshoot a little; closing must feel like the
    /// panel simply left, so it is critically damped.
    private var geometryAnimation: Animation? {
        guard !reduceMotion else { return nil }
        return isCollapsed
            ? .spring(response: 0.3, dampingFraction: 1)
            : .spring(response: 0.36, dampingFraction: 0.86)
    }

    private var backdrop: NotchBackdropShape {
        NotchBackdropShape(
            topShoulder: isCollapsed ? 6 : 22,
            bottomRadius: isCollapsed ? 14 : 28
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            Color.clear
                .frame(height: model.notchTopInset)

            if !isCollapsed {
                ExpandedContent(model: model, reduceMotion: reduceMotion)
                    .transition(reduceMotion
                                ? .opacity
                                : .opacity.combined(with: .scale(scale: 0.97, anchor: .top)))
                    .animation(reduceMotion
                               ? .easeOut(duration: microFade)
                               : .spring(response: 0.34, dampingFraction: 0.9),
                               value: isCollapsed)
            }
        }
        .frame(width: currentSize.width, height: currentSize.height, alignment: .top)
        .background {
            if reduceTransparency || contrast == .increased || isCollapsed {
                Color.black
            } else {
                LinearGradient(colors: [surfaceTopColor, surfaceBottomColor],
                               startPoint: .top, endPoint: .bottom)
            }
        }
        .clipShape(backdrop)
        // Collapsed, the panel must disappear into the bezel — an edge stroke there would draw it.
        .overlay { if !isCollapsed { backdrop.stroke(edgeStrokeColor, lineWidth: 1) } }
        .shadow(
            color: isCollapsed ? .clear : .black.opacity(0.5),
            radius: isCollapsed ? 0 : 22,
            y: isCollapsed ? 0 : 10
        )
        .animation(geometryAnimation, value: isCollapsed)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .environment(\.colorScheme, .dark)
    }
}
