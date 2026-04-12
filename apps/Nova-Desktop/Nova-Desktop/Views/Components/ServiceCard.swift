// ServiceCard.swift — Nova Desktop
// Reusable card for a single monitored service with status + control buttons.
// Written by Jordan Koch.

import SwiftUI

struct ServiceCard: View {
    let service: MonitoredService
    var compact: Bool = false
    @State private var confirmStop = false

    var stateColor: Color { ModernColors.statusColor(service.state) }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 6 : 8) {
            // Header row
            HStack(spacing: 8) {
                Image(systemName: service.icon)
                    .font(.system(size: compact ? 13 : 16, weight: .semibold))
                    .foregroundColor(stateColor)
                    .frame(width: compact ? 18 : 22)

                VStack(alignment: .leading, spacing: 1) {
                    Text(service.name)
                        .font(.system(size: compact ? 12 : 14, weight: .semibold, design: .rounded))
                        .foregroundColor(ModernColors.textPrimary)
                    if !service.detail.isEmpty {
                        Text(service.detail)
                            .font(.system(size: compact ? 10 : 11, design: .rounded))
                            .foregroundColor(ModernColors.textSecondary)
                            .lineLimit(1)
                    }
                }

                Spacer(minLength: 0)

                VStack(alignment: .trailing, spacing: 3) {
                    StatusDot(state: service.state, size: compact ? 7 : 9)
                    LatencyBadge(ms: service.latencyMs)
                }
            }

            // Control buttons
            if !compact {
                HStack(spacing: 6) {
                    if service.state == .offline || service.state == .unknown {
                        if let start = service.startAction {
                            ControlButton(label: "Start", icon: "play.fill", color: ModernColors.green) {
                                ServiceController.shared.perform(start, serviceId: service.id)
                            }
                        }
                    } else {
                        if let stop = service.stopAction {
                            if confirmStop {
                                ControlButton(label: "Confirm Stop", icon: "xmark", color: ModernColors.statusOffline) {
                                    ServiceController.shared.perform(stop, serviceId: service.id)
                                    confirmStop = false
                                }
                                ControlButton(label: "Cancel", icon: "arrow.uturn.backward", color: ModernColors.textTertiary) {
                                    confirmStop = false
                                }
                            } else {
                                ControlButton(label: "Stop", icon: "stop.fill", color: ModernColors.orange) {
                                    confirmStop = true
                                }
                                if service.startAction != nil {
                                    ControlButton(label: "Restart", icon: "arrow.clockwise", color: ModernColors.cyan) {
                                        ServiceController.shared.restart(service: service)
                                    }
                                }
                            }
                        }
                        if let open = service.openAction {
                            ControlButton(label: "Open", icon: "arrow.up.right.square", color: ModernColors.blue) {
                                ServiceController.shared.perform(open, serviceId: service.id)
                            }
                        }
                    }
                    Spacer(minLength: 0)

                    if let port = service.port {
                        Text(":\(port)")
                            .font(.system(size: 10, weight: .medium, design: .monospaced))
                            .foregroundColor(ModernColors.textTertiary)
                    }
                }
            }
        }
        .glassCard(prominent: service.state == .online, accent: stateColor)
    }
}

// MARK: - Compact Service Pill (for apps row)

struct ServicePill: View {
    let service: MonitoredService
    var stateColor: Color { ModernColors.statusColor(service.state) }

    var body: some View {
        HStack(spacing: 7) {
            StatusDot(state: service.state, size: 7)
            Image(systemName: service.icon)
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(stateColor)
            Text(service.name)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundColor(ModernColors.textPrimary)
            if let port = service.port {
                Text(":\(port)")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(ModernColors.textTertiary)
            }
            Spacer(minLength: 0)
            if service.state == .offline {
                if let start = service.startAction {
                    Button {
                        ServiceController.shared.perform(start, serviceId: service.id)
                    } label: {
                        Image(systemName: "play.fill")
                            .font(.system(size: 9))
                            .foregroundColor(ModernColors.green)
                    }
                    .buttonStyle(.plain)
                }
            } else if service.state == .online {
                if let stop = service.stopAction {
                    Button {
                        ServiceController.shared.perform(stop, serviceId: service.id)
                    } label: {
                        Image(systemName: "stop.fill")
                            .font(.system(size: 9))
                            .foregroundColor(ModernColors.orange)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 12)
            .fill(ModernColors.glassBackground)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(stateColor.opacity(0.25), lineWidth: 1)))
    }
}
