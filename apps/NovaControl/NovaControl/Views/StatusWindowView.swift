// NovaControl — Status Window View
// Written by Jordan Koch
// Main window shown when clicking the menu bar icon

import SwiftUI

struct StatusWindowView: View {
    @EnvironmentObject var data: DataManager
    @State private var selectedTab: Tab = .actionItems

    enum Tab: String, CaseIterable {
        case actionItems = "Action Items"
        case devices     = "Devices"
        case system      = "System"
        case news        = "News"
        case nova        = "Nova"
        case health      = "Health"
    }

    var body: some View {
        VStack(spacing: 0) {
            headerView
            Divider()
            serviceGrid
            Divider()
            tabBar
            tabContent
        }
        .frame(minWidth: 600, minHeight: 460)
        .background(Color(NSColor.windowBackgroundColor))
    }

    // MARK: - Header

    private var headerView: some View {
        HStack {
            Image(systemName: "antenna.radiowaves.left.and.right")
                .font(.title2)
                .foregroundColor(.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text("Nova Control")
                    .font(.headline)
                Text("port 37400 · unified API")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text("Last refresh")
                    .font(.caption2)
                    .foregroundColor(.secondary)
                Text(data.lastRefresh, style: .time)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Button {
                data.refresh()
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh all data")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    // MARK: - Service Status Grid

    private var serviceGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
            ForEach(data.serviceStatuses) { service in
                ServiceCard(service: service)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    // MARK: - Tab Bar

    private var tabBar: some View {
        HStack(spacing: 0) {
            ForEach(Tab.allCases, id: \.self) { tab in
                Button {
                    selectedTab = tab
                } label: {
                    Text(tab.rawValue)
                        .font(.subheadline)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(selectedTab == tab ? Color.accentColor.opacity(0.15) : Color.clear)
                        .foregroundColor(selectedTab == tab ? .accentColor : .primary)
                }
                .buttonStyle(.borderless)
                Divider().frame(height: 20)
            }
            Spacer()
        }
        .background(Color(NSColor.controlBackgroundColor))
    }

    // MARK: - Tab Content

    @ViewBuilder
    private var tabContent: some View {
        ScrollView {
            switch selectedTab {
            case .actionItems: ActionItemsTab()
            case .devices:     DevicesTab()
            case .system:      SystemTab()
            case .news:        NewsTab()
            case .nova:        NovaTab()
            case .health:      HealthTab()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Service Card

struct ServiceCard: View {
    let service: ServiceInfo

    var dotColor: Color {
        switch service.status {
        case .online:   return .green
        case .degraded: return .yellow
        case .offline:  return .red
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(dotColor)
                .frame(width: 8, height: 8)
                .shadow(color: dotColor.opacity(0.6), radius: 3)
            VStack(alignment: .leading, spacing: 2) {
                Text(service.name)
                    .font(.subheadline)
                    .fontWeight(.medium)
                Text(service.summary)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            Text(":\(service.oldPort)")
                .font(.caption2)
                .foregroundColor(.secondary)
                .monospacedDigit()
        }
        .padding(10)
        .background(Color(NSColor.controlBackgroundColor))
        .cornerRadius(8)
    }
}

// MARK: - Action Items Tab

struct ActionItemsTab: View {
    @EnvironmentObject var data: DataManager

    var openItems: [ActionItem] {
        data.actionItems.filter { !$0.isCompleted }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if openItems.isEmpty {
                emptyState(icon: "checkmark.circle", text: "No open action items")
            } else {
                ForEach(openItems) { item in
                    ActionItemRow(item: item, personName: data.personName(for: item.assigneeId))
                    Divider().padding(.leading, 16)
                }
            }
        }
        .padding(.vertical, 8)
    }
}

struct ActionItemRow: View {
    let item: ActionItem
    let personName: String?

    var priorityColor: Color {
        switch item.priority.lowercased() {
        case "high", "critical": return .red
        case "medium":           return .orange
        default:                 return .blue
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(priorityColor)
                .frame(width: 8, height: 8)
                .padding(.top, 5)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(.subheadline)
                HStack(spacing: 8) {
                    if let name = personName {
                        Label(name, systemImage: "person")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    if let due = item.dueDate {
                        Label {
                            Text(due, style: .date)
                        } icon: {
                            Image(systemName: "calendar")
                        }
                        .font(.caption)
                        .foregroundColor(due < Date() ? .red : .secondary)
                    }
                    Text(item.priority.capitalized)
                        .font(.caption2)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(priorityColor.opacity(0.15))
                        .foregroundColor(priorityColor)
                        .cornerRadius(3)
                }
            }
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
    }
}

// MARK: - Devices Tab

struct DevicesTab: View {
    @EnvironmentObject var data: DataManager

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if data.devices.isEmpty {
                emptyState(icon: "network", text: "No devices scanned yet")
            } else {
                ForEach(data.devices) { device in
                    DeviceRow(device: device,
                              threatCount: data.threats.filter { $0.affectedHost == device.ipAddress }.count)
                    Divider().padding(.leading, 16)
                }
            }
        }
        .padding(.vertical, 8)
    }
}

struct DeviceRow: View {
    let device: ScannedDevice
    let threatCount: Int

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: deviceIcon(for: device.deviceType))
                .frame(width: 20)
                .foregroundColor(.accentColor)

            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(device.ipAddress)
                        .font(.subheadline)
                        .monospacedDigit()
                    if !device.isWhitelisted {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundColor(.yellow)
                            .font(.caption)
                    }
                }
                if let hostname = device.hostname {
                    Text(hostname)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            Spacer()

            if let manufacturer = device.manufacturer {
                Text(manufacturer)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            if threatCount > 0 {
                Text("\(threatCount) threat\(threatCount == 1 ? "" : "s")")
                    .font(.caption2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.red.opacity(0.15))
                    .foregroundColor(.red)
                    .cornerRadius(4)
            }

            Text(device.lastSeen, style: .relative)
                .font(.caption2)
                .foregroundColor(.secondary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
    }

    private func deviceIcon(for type: String) -> String {
        switch type.lowercased() {
        case "router", "gateway": return "wifi.router"
        case "tv", "appletv":     return "tv"
        case "phone", "mobile":   return "iphone"
        case "laptop", "mac":     return "laptopcomputer"
        case "server":            return "server.rack"
        case "printer":           return "printer"
        default:                  return "network"
        }
    }
}

// MARK: - System Tab

struct SystemTab: View {
    @EnvironmentObject var data: DataManager

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let stats = data.systemStats {
                // Stats summary row
                HStack(spacing: 20) {
                    StatBadge(label: "CPU", value: "\(Int(stats.cpuUser + stats.cpuSystem))%",
                              color: cpuColor(stats.cpuUser + stats.cpuSystem))
                    StatBadge(label: "RAM",
                              value: String(format: "%.1f / %.1f GB", stats.memUsedGB, stats.memTotalGB),
                              color: ramColor(stats.memUsedGB / max(stats.memTotalGB, 1)))
                    StatBadge(label: "Uptime", value: formatUptime(stats.uptime), color: .blue)
                }
                .padding(.horizontal, 16)

                Divider()

                // Process list
                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        Text("Top Processes")
                            .font(.subheadline)
                            .fontWeight(.semibold)
                        Spacer()
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 6)

                    HStack {
                        Text("PID").frame(width: 50, alignment: .leading)
                        Text("Command").frame(maxWidth: .infinity, alignment: .leading)
                        Text("CPU%").frame(width: 55, alignment: .trailing)
                        Text("MEM%").frame(width: 55, alignment: .trailing)
                        Text("User").frame(width: 80, alignment: .trailing)
                    }
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 4)

                    ForEach(data.topProcesses.prefix(10)) { proc in
                        HStack {
                            Text("\(proc.pid)").frame(width: 50, alignment: .leading)
                            Text(proc.command).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                            Text(String(format: "%.1f", proc.cpuPercent)).frame(width: 55, alignment: .trailing)
                            Text(String(format: "%.1f", proc.memPercent)).frame(width: 55, alignment: .trailing)
                            Text(proc.user).frame(width: 80, alignment: .trailing).lineLimit(1)
                        }
                        .font(.caption)
                        .monospacedDigit()
                        .padding(.horizontal, 16)
                        .padding(.vertical, 3)
                        Divider().padding(.leading, 16)
                    }
                }
            } else {
                emptyState(icon: "cpu", text: "Loading system stats...")
            }
        }
        .padding(.vertical, 8)
    }

    private func cpuColor(_ pct: Double) -> Color {
        pct > 80 ? .red : pct > 50 ? .orange : .green
    }

    private func ramColor(_ ratio: Double) -> Color {
        ratio > 0.85 ? .red : ratio > 0.65 ? .orange : .green
    }

    private func formatUptime(_ ti: TimeInterval) -> String {
        let hours = Int(ti) / 3600
        let minutes = (Int(ti) % 3600) / 60
        if hours >= 24 {
            return "\(hours / 24)d \(hours % 24)h"
        }
        return "\(hours)h \(minutes)m"
    }
}

struct StatBadge: View {
    let label: String
    let value: String
    let color: Color

    var body: some View {
        VStack(spacing: 2) {
            Text(label)
                .font(.caption2)
                .foregroundColor(.secondary)
            Text(value)
                .font(.subheadline)
                .fontWeight(.semibold)
                .foregroundColor(color)
        }
        .padding(8)
        .background(color.opacity(0.08))
        .cornerRadius(8)
    }
}

// MARK: - News Tab

struct NewsTab: View {
    @EnvironmentObject var data: DataManager

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if data.breakingNews.isEmpty {
                emptyState(icon: "newspaper", text: "No unread articles")
            } else {
                ForEach(data.breakingNews) { article in
                    NewsRow(article: article)
                    Divider().padding(.leading, 16)
                }
            }
        }
        .padding(.vertical, 8)
    }
}

struct NewsRow: View {
    let article: NewsArticle

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(article.title)
                    .font(.subheadline)
                    .lineLimit(2)
                Spacer()
                if article.isFavorite {
                    Image(systemName: "star.fill")
                        .foregroundColor(.yellow)
                        .font(.caption)
                }
            }
            HStack(spacing: 8) {
                Text(article.source)
                    .font(.caption2)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.accentColor.opacity(0.12))
                    .foregroundColor(.accentColor)
                    .cornerRadius(3)
                Text(article.category.capitalized)
                    .font(.caption2)
                    .foregroundColor(.secondary)
                Spacer()
                Text(article.publishedAt, style: .relative)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onTapGesture {
            if let url = URL(string: article.url) {
                NSWorkspace.shared.open(url)
            }
        }
    }
}

// MARK: - Nova Tab

struct NovaTab: View {
    @EnvironmentObject var data: DataManager

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let nova = data.novaStatus {
                // AI Services health row
                aiServicesSection
                Divider()
                // Nova identity
                novaIdentitySection(nova: nova)
                Divider()
                // Cron health grid
                cronSection(crons: nova.crons)
            } else {
                emptyState(icon: "brain.head.profile", text: "Loading Nova status...")
            }
        }
        .padding(.vertical, 8)
    }

    // MARK: AI Services

    private var aiServicesSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("AI Services")
                .font(.subheadline)
                .fontWeight(.semibold)
                .padding(.horizontal, 16)
                .padding(.top, 6)

            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())],
                      spacing: 6) {
                ForEach(data.aiServices) { svc in
                    AIServiceBadge(service: svc)
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 8)
        }
    }

    // MARK: Nova Identity

    private func novaIdentitySection(nova: NovaStatus) -> some View {
        HStack(spacing: 20) {
            StatBadge(label: "Model",
                      value: shortModelName(nova.currentModel),
                      color: .purple)
            StatBadge(label: "Memories",
                      value: nova.memoriesCount > 0 ? "\(nova.memoriesCount)" : "—",
                      color: nova.memoryServerOnline ? .blue : .secondary)
            StatBadge(label: "Sessions",
                      value: "\(nova.activeSessions)",
                      color: .teal)
            StatBadge(label: "Gateway",
                      value: nova.gatewayOnline ? "online" : "offline",
                      color: nova.gatewayOnline ? .green : .red)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    // MARK: Cron Grid

    private func cronSection(crons: [NovaCronJob]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Cron Jobs")
                    .font(.subheadline)
                    .fontWeight(.semibold)
                Spacer()
                let errors = crons.filter { $0.status == "error" }.count
                if errors > 0 {
                    Text("\(errors) error\(errors == 1 ? "" : "s")")
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.red.opacity(0.15))
                        .foregroundColor(.red)
                        .cornerRadius(4)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 6)

            if crons.isEmpty {
                emptyState(icon: "clock", text: "No cron jobs found")
            } else {
                // Header row
                HStack {
                    Text("Status").frame(width: 50, alignment: .leading)
                    Text("Job").frame(maxWidth: .infinity, alignment: .leading)
                    Text("Last").frame(width: 70, alignment: .trailing)
                    Text("Next").frame(width: 70, alignment: .trailing)
                }
                .font(.caption2)
                .foregroundColor(.secondary)
                .padding(.horizontal, 16)
                .padding(.bottom, 3)

                ForEach(crons) { job in
                    CronJobRow(job: job)
                    Divider().padding(.leading, 66)
                }
            }
        }
    }

    private func shortModelName(_ model: String) -> String {
        // Strip "openrouter/" prefix and truncate
        let stripped = model.replacingOccurrences(of: "openrouter/", with: "")
        if stripped.count > 18 { return String(stripped.suffix(18)) }
        return stripped
    }
}

// MARK: - Cron Job Row

struct CronJobRow: View {
    let job: NovaCronJob

    var statusColor: Color {
        switch job.status {
        case "ok":      return .green
        case "error":   return .red
        case "skipped": return .yellow
        default:        return .secondary
        }
    }

    var statusIcon: String {
        switch job.status {
        case "ok":      return "checkmark.circle.fill"
        case "error":   return "xmark.circle.fill"
        case "skipped": return "minus.circle.fill"
        default:        return "circle"
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: statusIcon)
                .foregroundColor(statusColor)
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 1) {
                Text(job.name)
                    .font(.caption)
                    .lineLimit(1)
                Text(job.schedule)
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Text(job.lastRun)
                .font(.caption2)
                .foregroundColor(.secondary)
                .frame(width: 70, alignment: .trailing)
                .lineLimit(1)

            Text(job.nextRun)
                .font(.caption2)
                .foregroundColor(job.status == "error" ? .red : .secondary)
                .frame(width: 70, alignment: .trailing)
                .lineLimit(1)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 4)
    }
}

// MARK: - AI Service Badge

struct AIServiceBadge: View {
    let service: AIService

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(service.isOnline ? Color.green : Color.red)
                .frame(width: 7, height: 7)
                .shadow(color: (service.isOnline ? Color.green : Color.red).opacity(0.5), radius: 2)
            VStack(alignment: .leading, spacing: 1) {
                Text(service.name)
                    .font(.caption2)
                    .fontWeight(.medium)
                    .lineLimit(1)
                Text(service.detail)
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(8)
        .background(Color(NSColor.controlBackgroundColor))
        .cornerRadius(7)
    }
}

// MARK: - Health Tab

struct HealthTab: View {
    @EnvironmentObject var data: DataManager

    // MARK: Derived health signals

    private var overallStatus: ServiceStatus {
        if data.serviceStatuses.contains(where: { $0.status == .offline })  { return .offline  }
        if data.serviceStatuses.contains(where: { $0.status == .degraded }) { return .degraded }
        return .online
    }

    private var systemPressureLabel: String {
        guard let s = data.systemStats else { return "unknown" }
        let cpu = s.cpuUser + s.cpuSystem
        let ram = s.memUsedGB / max(s.memTotalGB, 1)
        if cpu > 80 || ram > 0.90 { return "critical" }
        if cpu > 50 || ram > 0.75 { return "elevated" }
        return "normal"
    }

    private var systemPressureColor: Color {
        switch systemPressureLabel {
        case "critical": return .red
        case "elevated": return .orange
        default:         return .green
        }
    }

    private var openActionCount: Int { data.actionItems.filter { !$0.isCompleted }.count }
    private var cronErrorCount: Int  { data.novaStatus?.crons.filter { $0.status == "error" }.count ?? 0 }
    private var threatCount: Int     { data.threats.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            overallBanner
            Divider()
            serviceTrafficLights
            Divider()
            localLLMSection
            Divider()
            systemPressureRow
            if threatCount > 0 || openActionCount > 0 || cronErrorCount > 0 {
                Divider()
                attentionRequired
            }
        }
        .padding(.vertical, 8)
    }

    // MARK: Overall Banner

    private var overallBanner: some View {
        let (label, color, icon): (String, Color, String) = {
            switch overallStatus {
            case .online:   return ("All Systems Operational", .green,  "checkmark.seal.fill")
            case .degraded: return ("Degraded — Attention needed", .orange, "exclamationmark.triangle.fill")
            case .offline:  return ("Outage — Service(s) down",    .red,    "xmark.circle.fill")
            }
        }()
        return HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundColor(color)
            VStack(alignment: .leading, spacing: 3) {
                Text(label)
                    .font(.subheadline).fontWeight(.semibold)
                    .foregroundColor(color)
                Text("System pressure: \(systemPressureLabel) · refreshed \(data.lastRefresh, style: .relative) ago")
                    .font(.caption).foregroundColor(.secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(color.opacity(0.07))
    }

    // MARK: Per-Service Traffic Lights

    private var serviceTrafficLights: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Services")
                .font(.caption).fontWeight(.semibold).foregroundColor(.secondary)
                .padding(.horizontal, 16).padding(.top, 8)
            ForEach(data.serviceStatuses) { svc in
                HStack(spacing: 10) {
                    trafficLight(svc.status)
                    Text(svc.name)
                        .font(.subheadline)
                    Spacer()
                    Text(svc.summary)
                        .font(.caption).foregroundColor(.secondary).lineLimit(1)
                }
                .padding(.horizontal, 16).padding(.vertical, 4)
                Divider().padding(.leading, 42)
            }
        }
    }

    @ViewBuilder
    private func trafficLight(_ status: ServiceStatus) -> some View {
        ZStack {
            Circle().fill(Color.gray.opacity(0.15)).frame(width: 22, height: 22)
            Circle().fill(statusColor(status)).frame(width: 12, height: 12)
                .shadow(color: statusColor(status).opacity(0.7), radius: 4)
        }
    }

    private func statusColor(_ s: ServiceStatus) -> Color {
        switch s { case .online: return .green; case .degraded: return .orange; case .offline: return .red }
    }

    // MARK: System Pressure

    private var systemPressureRow: some View {
        HStack(spacing: 16) {
            if let s = data.systemStats {
                pressureCell(label: "CPU",
                             value: "\(Int(s.cpuUser + s.cpuSystem))%",
                             color: s.cpuUser + s.cpuSystem > 80 ? .red : s.cpuUser + s.cpuSystem > 50 ? .orange : .green)
                pressureCell(label: "RAM",
                             value: String(format: "%.0f%%", (s.memUsedGB / max(s.memTotalGB, 1)) * 100),
                             color: s.memUsedGB / max(s.memTotalGB, 1) > 0.85 ? .red :
                                    s.memUsedGB / max(s.memTotalGB, 1) > 0.65 ? .orange : .green)
                pressureCell(label: "Disk R",
                             value: String(format: "%.1f MB/s", s.diskReadMBs),  color: .blue)
                pressureCell(label: "Disk W",
                             value: String(format: "%.1f MB/s", s.diskWriteMBs), color: .blue)
            } else {
                Text("System stats loading…").font(.caption).foregroundColor(.secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    private func pressureCell(label: String, value: String, color: Color) -> some View {
        VStack(spacing: 2) {
            Text(label).font(.caption2).foregroundColor(.secondary)
            Text(value).font(.caption).fontWeight(.semibold).foregroundColor(color)
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(color.opacity(0.08))
        .cornerRadius(7)
    }

    // MARK: Local LLMs

    private var localLLMSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Local LLMs")
                    .font(.caption).fontWeight(.semibold).foregroundColor(.secondary)
                Spacer()
                let loaded = data.localLLMs.filter(\.isLoaded).count
                let total = data.localLLMs.count
                if total > 0 {
                    Text("\(loaded)/\(total) loaded")
                        .font(.caption2)
                        .foregroundColor(loaded > 0 ? .green : .secondary)
                }
            }
            .padding(.horizontal, 16).padding(.top, 8)

            if data.localLLMs.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "cpu").foregroundColor(.secondary)
                    Text("No local models detected")
                        .font(.caption).foregroundColor(.secondary)
                }
                .padding(.horizontal, 16).padding(.vertical, 4)
            } else {
                ForEach(data.localLLMs) { llm in
                    HStack(spacing: 10) {
                        ZStack {
                            Circle().fill(Color.gray.opacity(0.15)).frame(width: 22, height: 22)
                            Circle().fill(llm.isLoaded ? Color.green : (llm.isAvailable ? Color.gray : Color.red))
                                .frame(width: 12, height: 12)
                                .shadow(color: (llm.isLoaded ? Color.green : Color.clear).opacity(0.7), radius: 4)
                        }
                        VStack(alignment: .leading, spacing: 1) {
                            Text(llm.name)
                                .font(.caption)
                                .fontWeight(llm.isLoaded ? .semibold : .regular)
                                .lineLimit(1)
                            HStack(spacing: 4) {
                                Text(llm.backend.uppercased())
                                    .font(.system(size: 8, weight: .bold))
                                    .padding(.horizontal, 4).padding(.vertical, 1)
                                    .background(llm.backend == "mlx" ? Color.purple.opacity(0.15) : Color.blue.opacity(0.15))
                                    .foregroundColor(llm.backend == "mlx" ? .purple : .blue)
                                    .cornerRadius(3)
                                Text(llm.detail)
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                                    .lineLimit(1)
                            }
                        }
                        Spacer()
                        if let size = llm.sizeGB {
                            Text(String(format: "%.1fGB", size))
                                .font(.caption2)
                                .foregroundColor(.secondary)
                        }
                        Text(llm.isLoaded ? "loaded" : "idle")
                            .font(.caption2)
                            .foregroundColor(llm.isLoaded ? .green : .secondary)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 3)
                    Divider().padding(.leading, 42)
                }
            }
        }
        .padding(.bottom, 4)
    }

    // MARK: Attention Required

    private var attentionRequired: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Needs Attention")
                .font(.caption).fontWeight(.semibold).foregroundColor(.secondary)
                .padding(.horizontal, 16).padding(.top, 8)
            if openActionCount > 0 {
                attentionRow(icon: "checklist", color: .orange,
                             text: "\(openActionCount) open action item\(openActionCount == 1 ? "" : "s")")
            }
            if cronErrorCount > 0 {
                attentionRow(icon: "exclamationmark.arrow.circlepath", color: .red,
                             text: "\(cronErrorCount) Nova cron job\(cronErrorCount == 1 ? "" : "s") in error state")
            }
            if threatCount > 0 {
                attentionRow(icon: "shield.slash", color: .red,
                             text: "\(threatCount) security threat\(threatCount == 1 ? "" : "s") detected")
            }
        }
        .padding(.bottom, 8)
    }

    private func attentionRow(icon: String, color: Color, text: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundColor(color).frame(width: 20)
            Text(text).font(.subheadline)
        }
        .padding(.horizontal, 16).padding(.vertical, 4)
    }
}

// MARK: - Shared Helpers

@ViewBuilder
func emptyState(icon: String, text: String) -> some View {
    VStack(spacing: 8) {
        Image(systemName: icon)
            .font(.largeTitle)
            .foregroundColor(.secondary)
        Text(text)
            .font(.subheadline)
            .foregroundColor(.secondary)
    }
    .frame(maxWidth: .infinity)
    .padding(.vertical, 40)
}
