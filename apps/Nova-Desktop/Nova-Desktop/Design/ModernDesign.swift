// ModernDesign.swift — Nova Desktop
// Design system adapted from TopGUI with Nova-specific additions.
// Written by Jordan Koch.

import SwiftUI

// MARK: - Colors

struct ModernColors {
    static let gradientStart = Color(red: 0.06, green: 0.08, blue: 0.18)
    static let gradientMid   = Color(red: 0.08, green: 0.12, blue: 0.24)
    static let gradientEnd   = Color(red: 0.10, green: 0.15, blue: 0.30)

    // Accent palette
    static let cyan    = Color(red: 0.30, green: 0.85, blue: 0.95)
    static let teal    = Color(red: 0.20, green: 0.80, blue: 0.80)
    static let purple  = Color(red: 0.60, green: 0.40, blue: 0.95)
    static let orange  = Color(red: 1.00, green: 0.60, blue: 0.20)
    static let yellow  = Color(red: 1.00, green: 0.85, blue: 0.30)
    static let pink    = Color(red: 1.00, green: 0.35, blue: 0.65)
    static let green   = Color(red: 0.30, green: 0.90, blue: 0.60)
    static let blue    = Color(red: 0.30, green: 0.70, blue: 1.00)
    static let nova    = Color(red: 0.55, green: 0.35, blue: 1.00)   // Nova signature purple

    // Status
    static let statusOnline   = Color(red: 0.30, green: 0.90, blue: 0.60)
    static let statusDegraded = Color(red: 1.00, green: 0.75, blue: 0.20)
    static let statusOffline  = Color(red: 1.00, green: 0.30, blue: 0.40)
    static let statusUnknown  = Color.white.opacity(0.35)

    // Text
    static let textPrimary   = Color.white
    static let textSecondary = Color.white.opacity(0.70)
    static let textTertiary  = Color.white.opacity(0.45)

    // Glass
    static let glassBackground = Color.white.opacity(0.05)
    static let glassBorder     = Color.white.opacity(0.14)

    // Blob background colors
    static let blobNova   = Color(red: 0.40, green: 0.20, blue: 0.75)
    static let blobCyan   = Color(red: 0.15, green: 0.60, blue: 0.85)
    static let blobPink   = Color(red: 0.85, green: 0.25, blue: 0.55)
    static let blobOrange = Color(red: 0.85, green: 0.45, blue: 0.15)

    static func statusColor(_ state: ServiceState) -> Color {
        switch state {
        case .online:   return statusOnline
        case .degraded: return statusDegraded
        case .offline:  return statusOffline
        case .unknown:  return statusUnknown
        }
    }

    static func heatColor(percentage: Double) -> Color {
        switch percentage {
        case 0..<30:  return statusOnline
        case 30..<60: return yellow
        case 60..<80: return orange
        default:      return statusOffline
        }
    }

    static var backgroundGradient: LinearGradient {
        LinearGradient(colors: [gradientStart, gradientMid, gradientEnd],
                       startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

// MARK: - GlassCard

struct GlassCard: ViewModifier {
    var prominent: Bool = false
    var accentColor: Color = ModernColors.glassBorder

    func body(content: Content) -> some View {
        content
            .padding(18)
            .background(
                RoundedRectangle(cornerRadius: 20)
                    .fill(ModernColors.glassBackground)
                    .background(RoundedRectangle(cornerRadius: 20).fill(.ultraThinMaterial).opacity(0.88))
                    .overlay(RoundedRectangle(cornerRadius: 20).stroke(
                        prominent ? accentColor.opacity(0.5) : ModernColors.glassBorder, lineWidth: prominent ? 1.5 : 1))
                    .shadow(color: .black.opacity(0.25), radius: 12, y: 6)
                    .shadow(color: .white.opacity(0.04), radius: 1, x: -1, y: -1)
            )
    }
}

extension View {
    func glassCard(prominent: Bool = false, accent: Color = ModernColors.glassBorder) -> some View {
        modifier(GlassCard(prominent: prominent, accentColor: accent))
    }
}

// MARK: - Status Dot

struct StatusDot: View {
    let state: ServiceState
    var size: CGFloat = 9
    @State private var pulse = false

    var color: Color { ModernColors.statusColor(state) }

    var body: some View {
        ZStack {
            if state == .online {
                Circle().fill(color.opacity(0.3))
                    .frame(width: size * 2.2, height: size * 2.2)
                    .scaleEffect(pulse ? 1 : 0.6)
                    .opacity(pulse ? 0 : 0.6)
                    .animation(.easeOut(duration: 1.6).repeatForever(autoreverses: false), value: pulse)
            }
            Circle().fill(color).frame(width: size, height: size)
                .shadow(color: color.opacity(0.8), radius: state == .online ? 4 : 2)
        }
        .onAppear { pulse = true }
    }
}

// MARK: - Circular Gauge

struct CircularGauge: View {
    let value: Double
    let color: Color
    var size: CGFloat = 80
    var lineWidth: CGFloat = 8
    var showValue: Bool = true
    var label: String? = nil
    @State private var animated: Double = 0

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.10), lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: min(animated / 100.0, 1.0))
                .stroke(color, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: color.opacity(0.6), radius: 5)
            if showValue {
                VStack(spacing: 1) {
                    Text("\(Int(animated))").font(.system(size: size > 60 ? 20 : 14, weight: .bold, design: .rounded))
                        .foregroundColor(ModernColors.textPrimary)
                    if let l = label {
                        Text(l).font(.system(size: 9, weight: .medium, design: .rounded))
                            .foregroundColor(ModernColors.textSecondary)
                    }
                }
            }
        }
        .frame(width: size, height: size)
        .onAppear { withAnimation(.spring(response: 1.1, dampingFraction: 0.8)) { animated = value } }
        .onChange(of: value) { _, v in withAnimation(.spring(response: 0.8, dampingFraction: 0.8)) { animated = v } }
    }
}

// MARK: - Mini Gauge

struct MiniGauge: View {
    let value: Double
    let color: Color
    @State private var animated: Double = 0

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.10), lineWidth: 4)
            Circle().trim(from: 0, to: min(animated / 100.0, 1.0))
                .stroke(color, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: color.opacity(0.5), radius: 3)
        }
        .frame(width: 36, height: 36)
        .onAppear { withAnimation(.spring(response: 1.1, dampingFraction: 0.8)) { animated = value } }
        .onChange(of: value) { _, v in withAnimation(.spring(response: 0.8)) { animated = v } }
    }
}

// MARK: - Control Button

struct ControlButton: View {
    let label: String
    let icon: String
    let color: Color
    let action: () -> Void
    @State private var pressed = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 11, weight: .semibold))
                Text(label).font(.system(size: 11, weight: .semibold, design: .rounded))
            }
            .foregroundColor(.white)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 8).fill(color.opacity(0.85)))
            .shadow(color: color.opacity(0.4), radius: pressed ? 2 : 5)
            .scaleEffect(pressed ? 0.95 : 1.0)
        }
        .buttonStyle(.plain)
        .pressEvents(onPress: { pressed = true }, onRelease: { pressed = false })
    }
}

// MARK: - Press Events Helper

extension View {
    func pressEvents(onPress: @escaping () -> Void, onRelease: @escaping () -> Void) -> some View {
        simultaneousGesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in onPress() }
            .onEnded   { _ in onRelease() })
    }
}

// MARK: - Section Header

struct SectionHeader: View {
    let title: String
    let icon: String
    var accent: Color = ModernColors.cyan

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon).foregroundColor(accent).font(.system(size: 14, weight: .semibold))
            Text(title).font(.system(size: 15, weight: .bold, design: .rounded)).foregroundColor(ModernColors.textPrimary)
            Spacer()
        }
    }
}

// MARK: - Floating Blob

struct FloatingBlob: View {
    let color: Color; let size: CGFloat; let x: CGFloat; let y: CGFloat; let duration: Double
    @State private var moved = false
    var body: some View {
        Circle()
            .fill(RadialGradient(colors: [color, color.opacity(0.5)], center: .center, startRadius: 0, endRadius: size / 2))
            .frame(width: size, height: size).blur(radius: 55)
            .offset(x: moved ? x + 40 : x, y: moved ? y + 30 : y)
            .onAppear { withAnimation(.easeInOut(duration: duration).repeatForever(autoreverses: true)) { moved = true } }
    }
}

// MARK: - Glassmorphic Background

struct GlassmorphicBackground: View {
    var body: some View {
        ZStack {
            ModernColors.backgroundGradient.ignoresSafeArea()
            FloatingBlob(color: ModernColors.blobNova,  size: 420, x: -180, y: -200, duration: 9)
            FloatingBlob(color: ModernColors.blobCyan,  size: 350, x:  160, y: -140, duration: 7)
            FloatingBlob(color: ModernColors.blobPink,  size: 380, x:  120, y:  280, duration: 10)
            FloatingBlob(color: ModernColors.blobOrange,size: 280, x: -200, y:  240, duration: 8)
            FloatingBlob(color: ModernColors.blobCyan.opacity(0.6), size: 220, x: 260, y: 80, duration: 6)
        }
    }
}

// MARK: - Latency Badge

struct LatencyBadge: View {
    let ms: Int?
    var body: some View {
        if let ms = ms {
            Text("\(ms)ms")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundColor(ms < 100 ? ModernColors.green : ms < 500 ? ModernColors.yellow : ModernColors.statusOffline)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: 5).fill(Color.white.opacity(0.08)))
        }
    }
}
