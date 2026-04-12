// ContentView.swift — Nova Desktop
// Main dashboard layout — TopGUI style glassmorphic dark dashboard.
// Written by Jordan Koch.

import SwiftUI

struct ContentView: View {
    @EnvironmentObject var monitor: NovaMonitor
    @State private var selectedTab: Tab = .dashboard

    enum Tab: String, CaseIterable {
        case dashboard = "Dashboard"
        case ai        = "AI Services"
        case apps      = "Apps"
        case crons     = "Cron Jobs"
        case github    = "GitHub"
    }

    var overallState: ServiceState {
        let all = monitor.aiServices + monitor.apps
        if !monitor.openClaw.gatewayOnline { return .offline }
        if all.contains(where: { $0.state == .offline }) { return .degraded }
        if all.contains(where: { $0.state == .unknown })  { return .unknown }
        return .online
    }

    var overallColor: Color { ModernColors.statusColor(overallState) }

    var body: some View {
        ZStack {
            GlassmorphicBackground()

            VStack(spacing: 0) {
                headerBar
                Divider().background(ModernColors.glassBorder).opacity(0.5)
                tabBar
                Divider().background(ModernColors.glassBorder).opacity(0.5)

                ScrollView {
                    Group {
                        switch selectedTab {
                        case .dashboard: dashboardTab
                        case .ai:        aiTab
                        case .apps:      appsTab
                        case .crons:     cronsTab
                        case .github:    githubTab
                        }
                    }
                    .padding(20)
                }
            }
        }
        .frame(minWidth: 1400, minHeight: 860)
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack(spacing: 14) {
            // Nova logo
            ZStack {
                Circle()
                    .fill(RadialGradient(colors: [ModernColors.nova, ModernColors.purple.opacity(0.5)],
                                        center: .center, startRadius: 0, endRadius: 18))
                    .frame(width: 36, height: 36)
                    .shadow(color: ModernColors.nova.opacity(0.6), radius: 8)
                Text("N")
                    .font(.system(size: 18, weight: .black, design: .rounded))
                    .foregroundColor(.white)
            }

            VStack(alignment: .leading, spacing: 1) {
                Text("Nova Desktop")
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                    .foregroundColor(ModernColors.textPrimary)
                Text("AI infrastructure monitor")
                    .font(.system(size: 11, design: .rounded))
                    .foregroundColor(ModernColors.textTertiary)
            }

            Spacer()

            // Overall health badge
            HStack(spacing: 6) {
                StatusDot(state: overallState, size: 8)
                Text(overallState.rawValue)
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundColor(overallColor)
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 20).fill(overallColor.opacity(0.12))
                .overlay(RoundedRectangle(cornerRadius: 20).stroke(overallColor.opacity(0.3), lineWidth: 1)))

            // Services online count
            let onlineCount = (monitor.aiServices + monitor.apps).filter { $0.state == .online }.count
            let totalCount  = monitor.aiServices.count + monitor.apps.count
            Text("\(onlineCount)/\(totalCount) services up")
                .font(.system(size: 12, design: .rounded))
                .foregroundColor(ModernColors.textSecondary)

            // Cron error indicator
            let cronErrors = monitor.openClaw.cronJobs.filter { $0.state == "error" }.count
            if cronErrors > 0 {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 10))
                        .foregroundColor(ModernColors.yellow)
                    Text("\(cronErrors) cron error\(cronErrors == 1 ? "" : "s")")
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundColor(ModernColors.yellow)
                }
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: 14).fill(ModernColors.yellow.opacity(0.10)))
            }

            Divider().frame(height: 24).opacity(0.3)

            // Last refresh
            VStack(alignment: .trailing, spacing: 1) {
                Text("Last refresh").font(.system(size: 9)).foregroundColor(ModernColors.textTertiary)
                Text(monitor.lastRefresh, style: .time)
                    .font(.system(size: 10, design: .rounded))
                    .foregroundColor(ModernColors.textSecondary)
            }

            // Refresh button
            Button {
                Task { await monitor.refresh() }
            } label: {
                Image(systemName: monitor.isRefreshing ? "arrow.clockwise" : "arrow.clockwise")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(ModernColors.cyan)
                    .rotationEffect(.degrees(monitor.isRefreshing ? 360 : 0))
                    .animation(monitor.isRefreshing ? .linear(duration: 1).repeatForever(autoreverses: false) : .default,
                               value: monitor.isRefreshing)
            }
            .buttonStyle(.plain)
            .help("Refresh all services (auto-refreshes every 10s)")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    // MARK: - Tab Bar

    private var tabBar: some View {
        HStack(spacing: 0) {
            ForEach(Tab.allCases, id: \.self) { tab in
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) { selectedTab = tab }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: tabIcon(tab))
                            .font(.system(size: 12, weight: .medium))
                        Text(tab.rawValue)
                            .font(.system(size: 13, weight: selectedTab == tab ? .semibold : .regular, design: .rounded))
                    }
                    .foregroundColor(selectedTab == tab ? ModernColors.cyan : ModernColors.textSecondary)
                    .padding(.horizontal, 18).padding(.vertical, 10)
                    .background(selectedTab == tab ? ModernColors.cyan.opacity(0.12) : Color.clear)
                }
                .buttonStyle(.plain)
            }
            Spacer()
        }
        .background(Color.white.opacity(0.02))
    }

    private func tabIcon(_ tab: Tab) -> String {
        switch tab {
        case .dashboard: return "rectangle.3.group.fill"
        case .ai:        return "cpu.fill"
        case .apps:      return "square.grid.2x2.fill"
        case .crons:     return "clock.fill"
        case .github:    return "arrow.triangle.branch"
        }
    }

    // MARK: - Tab Content

    private var dashboardTab: some View {
        VStack(spacing: 16) {
            // Row 1: OpenClaw core (takes 2/3) + System (1/3)
            HStack(alignment: .top, spacing: 12) {
                VStack(spacing: 16) {
                    HStack(alignment: .top, spacing: 12) {
                        gatewayMiniCard
                        memoryMiniCard
                        activityMiniCard
                    }
                    .frame(maxWidth: .infinity)
                }

                SystemSection()
                    .frame(width: 240)
            }

            // Row 2: AI services
            AIServicesSection()

            // Row 3: Apps + GitHub
            HStack(alignment: .top, spacing: 12) {
                AppsSection()
                    .frame(maxWidth: .infinity)
                GitHubSection()
                    .frame(maxWidth: .infinity)
            }

            // Row 4: Cron jobs (top 10 errors/recent)
            OpenClawSection()
        }
    }

    // Mini versions of gateway/memory/activity for dashboard row
    private var gatewayMiniCard: some View {
        HStack(spacing: 10) {
            StatusDot(state: monitor.openClaw.gatewayOnline ? .online : .offline)
            VStack(alignment: .leading, spacing: 3) {
                Text("Gateway").font(.system(size: 13, weight: .bold, design: .rounded)).foregroundColor(ModernColors.textPrimary)
                Text(monitor.openClaw.gatewayOnline ? "\(monitor.openClaw.activeSessions) sessions · \(monitor.openClaw.currentModel.components(separatedBy: "/").last ?? "—")"
                     : "offline")
                    .font(.system(size: 11, design: .rounded)).foregroundColor(ModernColors.textSecondary).lineLimit(1)
            }
            Spacer()
            if !monitor.openClaw.gatewayOnline {
                ControlButton(label: "Start", icon: "play.fill", color: ModernColors.green) {
                    ServiceController.shared.restartOpenClawGateway()
                }
            }
        }
        .glassCard(prominent: monitor.openClaw.gatewayOnline, accent: ModernColors.nova)
        .frame(maxWidth: .infinity)
    }

    private var memoryMiniCard: some View {
        HStack(spacing: 10) {
            StatusDot(state: monitor.openClaw.memoryServerOnline ? .online : .offline)
            VStack(alignment: .leading, spacing: 3) {
                Text("Memory").font(.system(size: 13, weight: .bold, design: .rounded)).foregroundColor(ModernColors.textPrimary)
                Text(monitor.openClaw.memoryServerOnline ? "\(monitor.openClaw.memoriesCount) memories" : "offline")
                    .font(.system(size: 11, design: .rounded)).foregroundColor(ModernColors.textSecondary)
            }
            Spacer()
        }
        .glassCard(prominent: monitor.openClaw.memoryServerOnline, accent: ModernColors.purple)
        .frame(maxWidth: .infinity)
    }

    private var activityMiniCard: some View {
        HStack(spacing: 10) {
            Image(systemName: "envelope.fill")
                .font(.system(size: 12)).foregroundColor(ModernColors.teal)
            VStack(alignment: .leading, spacing: 3) {
                Text("Email").font(.system(size: 13, weight: .bold, design: .rounded)).foregroundColor(ModernColors.textPrimary)
                Text(monitor.novaActivity.emailUnreadCount == 0 ? "inbox clear"
                     : "\(monitor.novaActivity.emailUnreadCount) unread")
                    .font(.system(size: 11, design: .rounded))
                    .foregroundColor(monitor.novaActivity.emailUnreadCount > 0 ? ModernColors.yellow : ModernColors.textSecondary)
            }
            Spacer()
        }
        .glassCard(accent: ModernColors.teal)
        .frame(maxWidth: .infinity)
    }

    private var aiTab: some View {
        AIServicesSection()
    }

    private var appsTab: some View {
        VStack(spacing: 12) {
            AppsSection()
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                ForEach(monitor.apps) { app in
                    ServiceCard(service: app)
                }
            }
        }
    }

    private var cronsTab: some View {
        OpenClawSection()
    }

    private var githubTab: some View {
        GitHubSection()
    }
}
