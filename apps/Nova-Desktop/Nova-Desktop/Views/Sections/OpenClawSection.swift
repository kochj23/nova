// OpenClawSection.swift — Nova Desktop
// OpenClaw gateway, memory server, and cron job table.
// Written by Jordan Koch.

import SwiftUI

struct OpenClawSection: View {
    @EnvironmentObject var monitor: NovaMonitor
    @State private var showAllCrons = false

    var crons: [CronJobStatus] { monitor.openClaw.cronJobs }
    var errorCrons: [CronJobStatus] { crons.filter { $0.state == "error" } }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "OpenClaw Core", icon: "bolt.fill", accent: ModernColors.nova)

            // Gateway + memory cards
            HStack(alignment: .top, spacing: 10) {
                gatewayCard
                memoryCard
                activityCard
            }

            // Cron jobs
            cronTable
        }
    }

    // MARK: Gateway Card

    private var gatewayCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "bolt.fill")
                    .foregroundColor(ModernColors.nova)
                    .font(.system(size: 16, weight: .semibold))
                Text("Gateway")
                    .font(.system(size: 14, weight: .bold, design: .rounded))
                    .foregroundColor(ModernColors.textPrimary)
                Spacer()
                StatusDot(state: monitor.openClaw.gatewayOnline ? .online : .offline)
            }

            VStack(alignment: .leading, spacing: 4) {
                metricRow("Version", monitor.openClaw.gatewayVersion)
                metricRow("Sessions", "\(monitor.openClaw.activeSessions)")
                metricRow("Model", monitor.openClaw.currentModel.components(separatedBy: "/").last ?? monitor.openClaw.currentModel)
                metricRow("Slack", monitor.openClaw.slackConnected ? "connected" : "disconnected",
                          color: monitor.openClaw.slackConnected ? ModernColors.green : ModernColors.statusOffline)
            }

            Spacer(minLength: 0)

            HStack(spacing: 6) {
                if !monitor.openClaw.gatewayOnline {
                    ControlButton(label: "Start", icon: "play.fill", color: ModernColors.green) {
                        ServiceController.shared.restartOpenClawGateway()
                    }
                } else {
                    ControlButton(label: "Restart", icon: "arrow.clockwise", color: ModernColors.cyan) {
                        ServiceController.shared.restartOpenClawGateway()
                    }
                }
                Spacer()
            }
        }
        .glassCard(prominent: monitor.openClaw.gatewayOnline, accent: ModernColors.nova)
        .frame(maxWidth: .infinity)
    }

    // MARK: Memory Card

    private var memoryCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "brain.head.profile")
                    .foregroundColor(ModernColors.purple)
                    .font(.system(size: 16, weight: .semibold))
                Text("Memory")
                    .font(.system(size: 14, weight: .bold, design: .rounded))
                    .foregroundColor(ModernColors.textPrimary)
                Spacer()
                StatusDot(state: monitor.openClaw.memoryServerOnline ? .online : .offline)
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("\(monitor.openClaw.memoriesCount)")
                        .font(.system(size: 26, weight: .bold, design: .rounded))
                        .foregroundColor(ModernColors.purple)
                    Text("memories")
                        .font(.system(size: 13, design: .rounded))
                        .foregroundColor(ModernColors.textSecondary)
                        .padding(.top, 8)
                }
                metricRow("Backend", "pgvector")
                metricRow("Redis queue", monitor.openClaw.redisOnline ? (monitor.openClaw.memoryQueueDepth == 0 ? "idle" : "\(monitor.openClaw.memoryQueueDepth) pending") : "offline",
                          color: monitor.openClaw.redisOnline ? ModernColors.green : ModernColors.statusOffline)
                metricRow("/search", monitor.openClaw.memorySearchEndpoint ? "available" : "unavailable",
                          color: monitor.openClaw.memorySearchEndpoint ? ModernColors.green : ModernColors.yellow)
            }

            Spacer(minLength: 0)

            HStack(spacing: 6) {
                if !monitor.openClaw.memoryServerOnline {
                    ControlButton(label: "Start", icon: "play.fill", color: ModernColors.green) {
                        ServiceController.shared.restartMemoryServer()
                    }
                } else {
                    ControlButton(label: "Restart", icon: "arrow.clockwise", color: ModernColors.purple) {
                        ServiceController.shared.restartMemoryServer()
                    }
                }
                Spacer()
            }
        }
        .glassCard(prominent: monitor.openClaw.memoryServerOnline, accent: ModernColors.purple)
        .frame(maxWidth: .infinity)
    }

    // MARK: Activity Card

    private var activityCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "waveform")
                    .foregroundColor(ModernColors.teal)
                    .font(.system(size: 16, weight: .semibold))
                Text("Nova Activity")
                    .font(.system(size: 14, weight: .bold, design: .rounded))
                    .foregroundColor(ModernColors.textPrimary)
                Spacer()
            }

            VStack(alignment: .leading, spacing: 4) {
                metricRow("Active sessions", "\(monitor.novaActivity.activeSessions)")
                metricRow("Email unread", "\(monitor.novaActivity.emailUnreadCount)",
                          color: monitor.novaActivity.emailUnreadCount > 0 ? ModernColors.yellow : ModernColors.textSecondary)
                metricRow("Cron errors", "\(monitor.novaActivity.cronErrorCount)",
                          color: monitor.novaActivity.cronErrorCount > 0 ? ModernColors.statusOffline : ModernColors.green)
                if let lastSlack = monitor.novaActivity.lastSlackMessageDate {
                    metricRow("Last Slack msg", relativeTime(lastSlack))
                }
                if let lastEmail = monitor.novaActivity.lastEmailCheck {
                    metricRow("Email checked", relativeTime(lastEmail))
                }
            }
            Spacer(minLength: 0)
        }
        .glassCard(accent: ModernColors.teal)
        .frame(maxWidth: .infinity)
    }

    // MARK: Cron Table

    private var cronTable: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                SectionHeader(title: "Cron Jobs (\(crons.count))", icon: "clock.fill", accent: ModernColors.cyan)
                if errorCrons.count > 0 {
                    Text("\(errorCrons.count) error\(errorCrons.count == 1 ? "" : "s")")
                        .font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(RoundedRectangle(cornerRadius: 6).fill(ModernColors.statusOffline.opacity(0.2)))
                        .foregroundColor(ModernColors.statusOffline)
                }
                Spacer()
                Button {
                    withAnimation { showAllCrons.toggle() }
                } label: {
                    Text(showAllCrons ? "Show errors only" : "Show all")
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundColor(ModernColors.cyan)
                }
                .buttonStyle(.plain)
            }

            VStack(spacing: 0) {
                // Header
                HStack(spacing: 0) {
                    Text("Status").frame(width: 60, alignment: .leading)
                    Text("Job").frame(maxWidth: .infinity, alignment: .leading)
                    Text("Schedule").frame(width: 140, alignment: .leading)
                    Text("Last").frame(width: 90, alignment: .trailing)
                    Text("Next").frame(width: 90, alignment: .trailing)
                    Text("").frame(width: 70)
                }
                .font(.system(size: 10, weight: .semibold, design: .rounded))
                .foregroundColor(ModernColors.textTertiary)
                .padding(.horizontal, 12).padding(.vertical, 6)

                Divider().background(ModernColors.glassBorder)

                let displayedCrons = showAllCrons ? crons : (errorCrons.isEmpty ? Array(crons.prefix(8)) : errorCrons)
                ForEach(displayedCrons) { cron in
                    CronRow(cron: cron)
                    if cron.id != displayedCrons.last?.id {
                        Divider().background(ModernColors.glassBorder).padding(.leading, 12)
                    }
                }

                if !showAllCrons && crons.count > (errorCrons.isEmpty ? 8 : errorCrons.count) {
                    Button {
                        withAnimation { showAllCrons = true }
                    } label: {
                        Text("Show \(crons.count - (errorCrons.isEmpty ? 8 : errorCrons.count)) more…")
                            .font(.system(size: 11, design: .rounded))
                            .foregroundColor(ModernColors.textTertiary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 8)
                    }
                    .buttonStyle(.plain)
                }
            }
            .background(RoundedRectangle(cornerRadius: 14).fill(ModernColors.glassBackground)
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(ModernColors.glassBorder, lineWidth: 1)))
        }
    }

    // MARK: Helpers

    private func metricRow(_ label: String, _ value: String, color: Color = ModernColors.textSecondary) -> some View {
        HStack {
            Text(label).font(.system(size: 11, design: .rounded)).foregroundColor(ModernColors.textTertiary)
            Spacer()
            Text(value).font(.system(size: 11, weight: .medium, design: .rounded)).foregroundColor(color).lineLimit(1)
        }
    }

    private func relativeTime(_ date: Date) -> String {
        let diff = Date().timeIntervalSince(date)
        if diff < 60 { return "just now" }
        if diff < 3600 { return "\(Int(diff/60))m ago" }
        return "\(Int(diff/3600))h ago"
    }
}

// MARK: - Cron Row

struct CronRow: View {
    let cron: CronJobStatus
    @EnvironmentObject var monitor: NovaMonitor

    var statusColor: Color {
        switch cron.state {
        case "ok":      return ModernColors.green
        case "error":   return ModernColors.statusOffline
        case "running": return ModernColors.cyan
        default:        return ModernColors.yellow
        }
    }

    var statusIcon: String {
        switch cron.state {
        case "ok":      return "checkmark.circle.fill"
        case "error":   return "xmark.circle.fill"
        case "running": return "arrow.triangle.2.circlepath"
        default:        return "minus.circle.fill"
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            Image(systemName: statusIcon).foregroundColor(statusColor)
                .font(.system(size: 12))
                .frame(width: 60, alignment: .leading)

            Text(cron.name)
                .font(.system(size: 12, design: .rounded))
                .foregroundColor(ModernColors.textPrimary)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(cron.schedule)
                .font(.system(size: 10, design: .monospaced))
                .foregroundColor(ModernColors.textTertiary)
                .lineLimit(1)
                .frame(width: 140, alignment: .leading)

            Text(cron.lastRun)
                .font(.system(size: 10, design: .rounded))
                .foregroundColor(ModernColors.textSecondary)
                .frame(width: 90, alignment: .trailing)

            Text(cron.nextRun)
                .font(.system(size: 10, design: .rounded))
                .foregroundColor(cron.state == "error" ? ModernColors.statusOffline : ModernColors.textSecondary)
                .frame(width: 90, alignment: .trailing)

            HStack(spacing: 4) {
                Button {
                    ServiceController.shared.shell("openclaw cron run \(cron.id) 2>/dev/null &")
                    Task { await monitor.refresh() }
                } label: {
                    Image(systemName: "play.fill")
                        .font(.system(size: 9))
                        .foregroundColor(ModernColors.cyan)
                        .padding(5)
                        .background(RoundedRectangle(cornerRadius: 5).fill(ModernColors.cyan.opacity(0.15)))
                }
                .buttonStyle(.plain)
                .help("Run now")
            }
            .frame(width: 70, alignment: .trailing)
        }
        .padding(.horizontal, 12).padding(.vertical, 5)
        .background(cron.consecutiveErrors > 0 ? ModernColors.statusOffline.opacity(0.05) : Color.clear)
    }
}
