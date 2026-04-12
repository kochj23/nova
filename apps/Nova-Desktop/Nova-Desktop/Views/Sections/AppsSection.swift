// AppsSection.swift — Nova Desktop
// Jordan's running apps with status pills and start/stop controls.
// Written by Jordan Koch.

import SwiftUI

struct AppsSection: View {
    @EnvironmentObject var monitor: NovaMonitor

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader(title: "Apps", icon: "square.grid.2x2.fill", accent: ModernColors.teal)

            LazyVGrid(columns: [GridItem(.adaptive(minimum: 170, maximum: 260))], spacing: 8) {
                ForEach(monitor.apps) { app in
                    ServicePill(service: app)
                }
            }
        }
    }
}
