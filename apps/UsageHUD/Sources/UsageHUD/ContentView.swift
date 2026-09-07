import AppKit
import SwiftUI

let warnColor = Color(red: 0xF5 / 255, green: 0x93 / 255, blue: 0x11 / 255)
let dangerColor = Color(red: 0xE5 / 255, green: 0x48 / 255, blue: 0x4D / 255)
let warnTextColor = warnColor
let dangerTextColor = dangerColor
let claudeColor = Color(red: 0xD9 / 255, green: 0x77 / 255, blue: 0x57 / 255)
let codexColor = Color(red: 0x2D / 255, green: 0x7F / 255, blue: 0xF9 / 255)
let microFade = 0.14

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

    private var geometryAnimation: Animation? {
        guard !reduceMotion else { return nil }
        return .spring(response: 0.32, dampingFraction: 1)
    }

    private var contentAnimation: Animation {
        reduceMotion ? .easeOut(duration: microFade) : .easeOut(duration: 0.2)
    }

    var body: some View {
        VStack(spacing: 0) {
            Color.clear
                .frame(height: model.notchTopInset)

            if !isCollapsed {
                ExpandedContent(model: model, reduceMotion: reduceMotion)
                    .transition(reduceMotion ? .identity : .opacity)
                    .animation(contentAnimation, value: isCollapsed)
            }
        }
        .frame(width: currentSize.width, height: currentSize.height, alignment: .top)
        .background {
            if reduceTransparency || contrast == .increased || isCollapsed {
                Color.black
            } else {
                LinearGradient(colors: [Color(white: 0.095), Color(white: 0.055)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
            }
        }
        .clipShape(
            NotchBackdropShape(
                topShoulder: isCollapsed ? 6 : 18,
                bottomRadius: isCollapsed ? 14 : 24
            )
        )
        .shadow(
            color: isCollapsed ? .clear : .black.opacity(0.65),
            radius: isCollapsed ? 0 : 12,
            y: isCollapsed ? 0 : 6
        )
        .animation(geometryAnimation, value: isCollapsed)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .environment(\.colorScheme, .dark)
    }
}
