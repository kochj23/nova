// GitHubSection.swift — Nova Desktop
// GitHub repo status for key Nova/Jordan repos.
// Written by Jordan Koch.

import SwiftUI
import AppKit

struct GitHubSection: View {
    @EnvironmentObject var monitor: NovaMonitor

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionHeader(title: "GitHub", icon: "arrow.triangle.branch", accent: ModernColors.green)
                Spacer()
                Button {
                    Task { await monitor.refreshGitHub() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 12))
                        .foregroundColor(ModernColors.textTertiary)
                }
                .buttonStyle(.plain)
            }

            if monitor.githubRepos.isEmpty {
                Text("Loading repos…")
                    .font(.system(size: 12, design: .rounded))
                    .foregroundColor(ModernColors.textTertiary)
                    .padding(.vertical, 20)
                    .frame(maxWidth: .infinity)
            } else {
                VStack(spacing: 6) {
                    ForEach(monitor.githubRepos) { repo in
                        RepoRow(repo: repo)
                        if repo.id != monitor.githubRepos.last?.id {
                            Divider().background(ModernColors.glassBorder)
                        }
                    }
                }
                .glassCard()
            }
        }
    }
}

// MARK: - Repo Row

struct RepoRow: View {
    let repo: GitHubRepoStatus

    var body: some View {
        HStack(spacing: 12) {
            // Repo icon + name
            HStack(spacing: 6) {
                Image(systemName: repo.isPrivate ? "lock.fill" : "chevron.left.forwardslash.chevron.right")
                    .font(.system(size: 12))
                    .foregroundColor(repo.isPrivate ? ModernColors.orange : ModernColors.green)
                    .frame(width: 18)

                VStack(alignment: .leading, spacing: 2) {
                    Text(repo.name)
                        .font(.system(size: 13, weight: .semibold, design: .rounded))
                        .foregroundColor(ModernColors.textPrimary)
                    Text(repo.description.isEmpty ? repo.fullName : repo.description)
                        .font(.system(size: 10, design: .rounded))
                        .foregroundColor(ModernColors.textTertiary)
                        .lineLimit(1)
                }
            }
            .frame(width: 180, alignment: .leading)

            // Last commit
            VStack(alignment: .leading, spacing: 1) {
                Text(repo.lastCommitMessage)
                    .font(.system(size: 11, design: .rounded))
                    .foregroundColor(ModernColors.textSecondary)
                    .lineLimit(1)
                if let date = repo.lastCommitDate {
                    Text(relativeTime(date))
                        .font(.system(size: 10, design: .rounded))
                        .foregroundColor(ModernColors.textTertiary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            // Badges
            HStack(spacing: 8) {
                if repo.stars > 0 {
                    badge("star.fill", "\(repo.stars)", color: ModernColors.yellow)
                }
                badge("exclamationmark.circle", "\(repo.openIssues)", color: repo.openIssues > 0 ? ModernColors.orange : ModernColors.textTertiary)
                if repo.openPRs > 0 {
                    badge("arrow.triangle.pull", "\(repo.openPRs)", color: ModernColors.cyan)
                }
            }

            // Open on GitHub
            Button {
                NSWorkspace.shared.open(URL(string: "https://github.com/\(repo.fullName)")!)
            } label: {
                Image(systemName: "arrow.up.right.square")
                    .font(.system(size: 12))
                    .foregroundColor(ModernColors.textTertiary)
            }
            .buttonStyle(.plain)
            .help("Open \(repo.fullName) on GitHub")
        }
        .padding(.vertical, 4)
    }

    private func badge(_ icon: String, _ value: String, color: Color) -> some View {
        HStack(spacing: 3) {
            Image(systemName: icon).font(.system(size: 9))
            Text(value).font(.system(size: 10, weight: .medium, design: .rounded))
        }
        .foregroundColor(color)
        .padding(.horizontal, 6).padding(.vertical, 2)
        .background(RoundedRectangle(cornerRadius: 5).fill(color.opacity(0.12)))
    }

    private func relativeTime(_ date: Date) -> String {
        let diff = Date().timeIntervalSince(date)
        if diff < 60 { return "just now" }
        if diff < 3600 { return "\(Int(diff/60))m ago" }
        if diff < 86400 { return "\(Int(diff/3600))h ago" }
        return "\(Int(diff/86400))d ago"
    }
}
