// AIServicesSection.swift — Nova Desktop
// Grid of all AI model services with status + controls.
// Written by Jordan Koch.

import SwiftUI

struct AIServicesSection: View {
    @EnvironmentObject var monitor: NovaMonitor

    private let columns = [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible()),
                           GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible()),
                           GridItem(.flexible())]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader(title: "AI Services", icon: "cpu.fill", accent: ModernColors.cyan)

            LazyVGrid(columns: columns, spacing: 8) {
                ForEach(monitor.aiServices) { svc in
                    AIServiceCard(service: svc)
                }
            }

            // Ollama model list (when online and models present)
            if !monitor.ollamaModels.isEmpty {
                ollamaModelsSection
            }
        }
    }

    private var ollamaModelsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Ollama Models (\(monitor.ollamaModels.count))")
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundColor(ModernColors.textSecondary)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(monitor.ollamaModels) { model in
                        HStack(spacing: 6) {
                            Image(systemName: "cube.fill")
                                .font(.system(size: 10))
                                .foregroundColor(ModernColors.orange)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(model.name)
                                    .font(.system(size: 11, weight: .medium, design: .rounded))
                                    .foregroundColor(ModernColors.textPrimary)
                                Text(model.size)
                                    .font(.system(size: 9, design: .monospaced))
                                    .foregroundColor(ModernColors.textTertiary)
                            }
                        }
                        .padding(.horizontal, 10).padding(.vertical, 7)
                        .background(RoundedRectangle(cornerRadius: 10)
                            .fill(ModernColors.glassBackground)
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(ModernColors.glassBorder, lineWidth: 1)))
                    }
                }
            }
        }
    }
}

// MARK: - AI Service Card

struct AIServiceCard: View {
    let service: MonitoredService
    @State private var showConfirmStop = false

    var accent: Color {
        switch service.id {
        case "ollama":      return ModernColors.orange
        case "mlxcode":     return ModernColors.purple
        case "openrouter":  return ModernColors.cyan
        case "openwebui":   return ModernColors.teal
        case "tinychat":    return ModernColors.yellow
        case "swarmui":     return ModernColors.pink
        case "comfyui":     return ModernColors.green
        default:            return ModernColors.blue
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: service.icon)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(accent)
                Spacer()
                StatusDot(state: service.state, size: 8)
            }

            Text(service.name)
                .font(.system(size: 13, weight: .bold, design: .rounded))
                .foregroundColor(ModernColors.textPrimary)

            if !service.detail.isEmpty {
                Text(service.detail)
                    .font(.system(size: 10, design: .rounded))
                    .foregroundColor(ModernColors.textSecondary)
                    .lineLimit(2)
            } else {
                Text(service.state.rawValue)
                    .font(.system(size: 10, design: .rounded))
                    .foregroundColor(ModernColors.statusColor(service.state))
            }

            if let ms = service.latencyMs {
                LatencyBadge(ms: ms)
            }

            Spacer(minLength: 4)

            // Controls
            if service.state == .offline || service.state == .unknown {
                if let start = service.startAction {
                    controlRow(label: "Start", icon: "play.fill", color: ModernColors.green) {
                        ServiceController.shared.perform(start, serviceId: service.id)
                    }
                }
            } else {
                HStack(spacing: 4) {
                    if let stop = service.stopAction {
                        if showConfirmStop {
                            controlRow(label: "Confirm", icon: "xmark", color: ModernColors.statusOffline) {
                                ServiceController.shared.perform(stop, serviceId: service.id)
                                showConfirmStop = false
                            }
                        } else {
                            controlRow(label: "Stop", icon: "stop.fill", color: ModernColors.orange) {
                                showConfirmStop = true
                            }
                        }
                    }
                    if let open = service.openAction {
                        Button {
                            ServiceController.shared.perform(open, serviceId: service.id)
                        } label: {
                            Image(systemName: "arrow.up.right.square")
                                .font(.system(size: 11))
                                .foregroundColor(ModernColors.blue)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(12)
        .frame(minHeight: 140)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(ModernColors.glassBackground)
                .background(RoundedRectangle(cornerRadius: 16).fill(.ultraThinMaterial).opacity(0.85))
                .overlay(RoundedRectangle(cornerRadius: 16)
                    .stroke(service.state == .online ? accent.opacity(0.35) : ModernColors.glassBorder, lineWidth: 1.5))
                .shadow(color: service.state == .online ? accent.opacity(0.15) : .clear, radius: 8)
        )
    }

    private func controlRow(label: String, icon: String, color: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: icon).font(.system(size: 10, weight: .semibold))
                Text(label).font(.system(size: 10, weight: .semibold, design: .rounded))
            }
            .foregroundColor(.white)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 7).fill(color.opacity(0.8)))
        }
        .buttonStyle(.plain)
    }
}
