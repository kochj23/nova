// SystemSection.swift — Nova Desktop
// Mini system stats card — CPU, RAM, disk I/O.
// Written by Jordan Koch.

import SwiftUI

struct SystemSection: View {
    @EnvironmentObject var monitor: NovaMonitor

    var stats: SystemStats { monitor.systemStats }

    var cpuColor:  Color { ModernColors.heatColor(percentage: stats.cpuPercent) }
    var ramColor:  Color { ModernColors.heatColor(percentage: stats.ramPercent) }
    var diskColor: Color { ModernColors.blue }

    private func formatUptime(_ ti: TimeInterval) -> String {
        let h = Int(ti) / 3600
        let m = (Int(ti) % 3600) / 60
        return h >= 24 ? "\(h/24)d \(h%24)h" : "\(h)h \(m)m"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "System", icon: "desktopcomputer", accent: ModernColors.blue)

            HStack(spacing: 16) {
                VStack(spacing: 6) {
                    CircularGauge(value: stats.cpuPercent, color: cpuColor, size: 72, lineWidth: 7, label: "CPU")
                    Text("\(Int(stats.cpuPercent))%")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundColor(cpuColor)
                }

                VStack(spacing: 6) {
                    CircularGauge(value: stats.ramPercent, color: ramColor, size: 72, lineWidth: 7, label: "RAM")
                    Text("\(Int(stats.ramPercent))%")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundColor(ramColor)
                }

                Spacer()

                VStack(alignment: .trailing, spacing: 6) {
                    diskStat("↑", "\(String(format: "%.1f", stats.diskWriteMBs)) MB/s", color: ModernColors.orange)
                    diskStat("↓", "\(String(format: "%.1f", stats.diskReadMBs)) MB/s", color: ModernColors.cyan)
                    Spacer()
                    Text("up \(formatUptime(stats.uptimeSeconds))")
                        .font(.system(size: 10, design: .rounded))
                        .foregroundColor(ModernColors.textTertiary)
                }
            }
        }
        .glassCard(accent: ModernColors.blue)
    }

    private func diskStat(_ arrow: String, _ value: String, color: Color) -> some View {
        HStack(spacing: 4) {
            Text(arrow)
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(color)
            Text(value)
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundColor(ModernColors.textSecondary)
        }
    }
}
