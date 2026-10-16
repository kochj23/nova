//
//  MacSettings.swift
//  Nova Cluster (Mac)
//
//  The standard macOS Settings window (⌘,): hub URL + viewer node (UserDefaults) and
//  the token (login Keychain, service net.digitalnoise.NovaCluster, account cluster-token).
//

import SwiftUI

struct MacSettingsView: View {
    let cluster: ClusterModel
    @State private var draft = ClusterConfig.current
    @State private var saved = false

    var body: some View {
        Form {
            Section {
                TextField("Hub URL", text: $draft.hub, prompt: Text("http://hub-host:port"))
                TextField("Node", text: $draft.node, prompt: Text(ClusterConfig.defaultNode))
                SecureField("Token", text: $draft.token)
            } header: {
                Text("Nova's hub")
            } footer: {
                Text("The token is kept in your login Keychain.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            .autocorrectionDisabled()

            Section("Status") {
                LabeledContent("Connection", value: cluster.isOffline ? "unreachable (\(cluster.lastError ?? "no reply"))" : "live")
                LabeledContent("Last heard", value: cluster.lastSuccess.map { Fmt.clock($0) } ?? "never")
                LabeledContent("Machines", value: "\(cluster.snapshot?.allNodes.count ?? 0)")
            }

            HStack {
                if saved { Text("Saved").foregroundStyle(.secondary) }
                Spacer()
                Button("Revert") { draft = .current; saved = false }
                    .disabled(draft == .current)
                Button("Save") {
                    draft.save(); draft = .current; cluster.restart(); saved = true
                }
                .keyboardShortcut(.defaultAction)
                .disabled(draft == .current)
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
        .preferredColorScheme(.dark)
        .onChange(of: draft) { _, _ in saved = false }
    }
}
