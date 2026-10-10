import SwiftUI
import UIKit

enum XTheme {
    static let background = adaptive(0xFAF7F1, 0x0D1213, lightContrast: 0xFFFFFF, darkContrast: 0x070B0C)
    static let surface = adaptive(0xFFFDF8, 0x151D1F, lightContrast: 0xFFFFFF, darkContrast: 0x11191B)
    static let elevated = adaptive(0xFFFDF8, 0x1E2A2C, lightContrast: 0xFFFFFF, darkContrast: 0x1E2A2C)
    static let text = adaptive(0x2F2925, 0xE8EFED, lightContrast: 0x181613, darkContrast: 0xFFFFFF)
    static let secondary = adaptive(0x746559, 0xA1B1AD, lightContrast: 0x4E4035, darkContrast: 0xCFDBD6)
    static let accent = adaptive(0x176F66, 0x72D0BD, lightContrast: 0x105B53, darkContrast: 0x94E9D5)
    static let onAccent = adaptive(0xFFFFFF, 0x092720, lightContrast: 0xFFFFFF, darkContrast: 0x051A15)
    static let soft = adaptive(0xEAF0E8, 0x1A302D, lightContrast: 0xDCEBDD, darkContrast: 0x213F38)
    static let border = adaptive(0xE4DED2, 0x2E3D3E, lightContrast: 0x9F9483, darkContrast: 0x687F7C)
    static let success = adaptive(0x24764A, 0x83D29E, lightContrast: 0x175D37, darkContrast: 0xA3EDB9)
    static let warning = adaptive(0x976217, 0xE8B567, lightContrast: 0x70460C, darkContrast: 0xFFD08A)
    static let error = adaptive(0xB74642, 0xF49891, lightContrast: 0x942C29, darkContrast: 0xFFB6AF)
    static let successBackground = adaptive(0xE9F1ED, 0x1A302D, lightContrast: 0xE2EDE7, darkContrast: 0x173424)
    static let warningBackground = adaptive(0xFAF3E5, 0x32291A, lightContrast: 0xEEDDBC, darkContrast: 0x483717)
    static let errorBackground = adaptive(0xFBEFEA, 0x332323, lightContrast: 0xF1DCD6, darkContrast: 0x4A2827)

    private static func adaptive(_ light: UInt32, _ dark: UInt32, lightContrast: UInt32, darkContrast: UInt32) -> Color {
        Color(uiColor: UIColor { traits in
            let increased = traits.accessibilityContrast == .high
            let hex = traits.userInterfaceStyle == .dark
                ? (increased ? darkContrast : dark)
                : (increased ? lightContrast : light)
            return UIColor(
                red: CGFloat((hex >> 16) & 0xFF) / 255,
                green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255,
                alpha: 1
            )
        })
    }
}

// The provisional brand palette stays the same in both interface appearances.
enum XBrand {
    static let background = Color(.sRGB, red: 26.0 / 255, green: 48.0 / 255, blue: 45.0 / 255, opacity: 1)
    static let secondary = Color(.sRGB, red: 161.0 / 255, green: 177.0 / 255, blue: 173.0 / 255, opacity: 1)
    static let accent = Color(.sRGB, red: 114.0 / 255, green: 208.0 / 255, blue: 189.0 / 255, opacity: 1)
}

// Two crossing paths form an original mark; functional icons remain SF Symbols.
struct XVISMark: View {
    var size: CGFloat = 44

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
                .fill(XBrand.background)
            XVISTrail(ascending: false)
                .stroke(XBrand.secondary, style: StrokeStyle(lineWidth: size * 0.115, lineCap: .round, lineJoin: .round))
            XVISTrail(ascending: true)
                .stroke(XBrand.accent, style: StrokeStyle(lineWidth: size * 0.115, lineCap: .round, lineJoin: .round))
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

private struct XVISTrail: Shape {
    var ascending: Bool

    func path(in rect: CGRect) -> Path {
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + rect.width * x, y: rect.minY + rect.height * y)
        }
        let startY: CGFloat = ascending ? 0.76 : 0.24
        let endY: CGFloat = ascending ? 0.24 : 0.76
        var path = Path()
        path.move(to: point(0.24, startY))
        path.addCurve(
            to: point(0.50, 0.50),
            control1: point(0.38, startY),
            control2: point(0.41, 0.50)
        )
        path.addCurve(
            to: point(0.76, endY),
            control1: point(0.59, 0.50),
            control2: point(0.62, endY)
        )
        return path
    }
}

struct XIconButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(XTheme.text)
            .frame(minWidth: 44, minHeight: 44)
            .background(configuration.isPressed ? XTheme.soft : Color.clear, in: Circle())
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.97 : 1)
            .opacity(isEnabled ? 1 : 0.5)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.10), value: configuration.isPressed)
    }
}

struct XPressButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .contentShape(Rectangle())
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.97 : 1)
            .opacity(isEnabled ? (configuration.isPressed ? 0.92 : 1) : 0.5)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.10), value: configuration.isPressed)
    }
}

struct XStatusPill: View {
    let status: String

    private var presentation: (label: String, icon: String, foreground: Color, background: Color, accessibility: String) {
        switch status {
        case "queued":
            return ("待执行", "clock", XTheme.secondary, XTheme.soft, "等待设备执行")
        case "assigned":
            return ("正在准备", "arrow.triangle.2.circlepath", XTheme.accent, XTheme.soft, "正在准备任务")
        case "awaiting_input":
            return ("待我处理", "hand.raised", XTheme.warning, XTheme.warningBackground, "等待中途交互完成")
        case "running":
            return ("执行中", "circle.dotted", XTheme.accent, XTheme.soft, "任务执行中")
        case "cancel_requested":
            return ("已请求停止", "pause.circle", XTheme.warning, XTheme.warningBackground, "已请求停止，等待设备确认")
        case "done":
            return ("已完成", "checkmark.circle.fill", XTheme.success, XTheme.successBackground, "任务已完成")
        case "failed":
            return ("执行失败", "exclamationmark.circle.fill", XTheme.error, XTheme.errorBackground, "任务执行失败")
        case "timed_out":
            return ("执行超时", "clock", XTheme.warning, XTheme.warningBackground, "任务执行超时")
        case "cancelled":
            return ("已取消", "stop.circle", XTheme.secondary, XTheme.soft, "任务已取消")
        case "interrupted":
            return ("执行中断", "pause.circle.fill", XTheme.warning, XTheme.warningBackground, "任务执行中断，请在执行设备上检查")
        default:
            return ("正在核对", "questionmark.circle", XTheme.secondary, XTheme.soft, "正在核对任务状态")
        }
    }

    var body: some View {
        let value = presentation
        HStack(spacing: 4) {
            Image(systemName: value.icon).accessibilityHidden(true)
            Text(value.label).fixedSize(horizontal: false, vertical: true)
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(value.foreground)
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(value.background, in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(value.accessibility)
    }
}

struct XControlSurface: ViewModifier {
    var cornerRadius: CGFloat = 26
    var active: Bool = false
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorSchemeContrast) private var contrast

    func body(content: Content) -> some View {
        surface(content)
            .overlay {
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(active ? XTheme.accent : XTheme.border, lineWidth: contrast == .increased ? 1.5 : 0.75)
                    .allowsHitTesting(false)
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: active)
    }

    @ViewBuilder private func surface(_ content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        if reduceTransparency || contrast == .increased {
            content.background(XTheme.elevated, in: shape)
        } else if #available(iOS 26.0, *) {
            content.glassEffect(.regular.tint(active ? XTheme.soft : nil), in: shape)
        } else {
            content.background(XTheme.surface, in: shape)
        }
    }
}

extension View {
    // Apply once to the outer navigation/composer surface, never to its nested controls.
    func xControlSurface(cornerRadius: CGFloat = 26, active: Bool = false) -> some View {
        modifier(XControlSurface(cornerRadius: cornerRadius, active: active))
    }
}
