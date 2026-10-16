//
//  MacSettings.swift
//  Nova Minion (Mac)
//
//  The standard macOS Settings window (⌘,): hub URL + node (UserDefaults) and the
//  minion token (login Keychain, service net.digitalnoise.NovaMinionMac).
//

import SwiftUI

struct MacSettingsView: View {
    let fleet: FleetModel
    @State private var draft = MinionConfig.current
    @State private var saved = false

    var body: some View {
        Form {
            Section {
                TextField("Hub URL", text: $draft.hub, prompt: Text("http://hub-host:port"))
                TextField("Node", text: $draft.node, prompt: Text(MinionConfig.defaultNode))
                SecureField("Minion token", text: $draft.token)
            } header: {
                Text("Nova's hub")
            } footer: {
                Text("The token is kept in your login Keychain.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            .autocorrectionDisabled()

            Section("Status") {
                LabeledContent("Connection", value: fleet.isOffline ? "resting (\(fleet.lastError ?? "no reply"))" : "live")
                LabeledContent("Last heard", value: fleet.lastSuccess.map { Fmt.clock($0) } ?? "never")
                LabeledContent("Minions", value: "\(fleet.snapshot?.fleet.count ?? 0)")
            }

            HStack {
                if saved { Text("Saved").foregroundStyle(.secondary) }
                Spacer()
                Button("Revert") { draft = .current; saved = false }
                    .disabled(draft == .current)
                Button("Save") {
                    draft.save(); draft = .current; fleet.restart(); saved = true
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
