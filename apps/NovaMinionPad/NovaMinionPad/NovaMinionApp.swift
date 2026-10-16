//
//  NovaMinionApp.swift
//  Nova Minion (iPad)
//
//  Display-only iPad companion to the Nova Minion screensaver: shows what
//  Nova's minion fleet is reading and learning, polled from Nova's hub.
//

import SwiftUI

@main
struct NovaMinionApp: App {
    init() {
        // First-launch setup from NOVA_MINION_HUB / _NODE / _TOKEN, if present.
        // `-demoMinions N` and `-simLandscape YES` arrive via the UserDefaults argument domain.
        MinionConfig.applyEnvironment()
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if UserDefaults.standard.bool(forKey: "simLandscape") {
                    // Screenshot aid for simulators that cannot rotate: lay out at
                    // landscape size, rotated into the portrait window.
                    GeometryReader { g in
                        ContentView()
                            .frame(width: g.size.height, height: g.size.width)
                            .rotationEffect(.degrees(-90))
                            .position(x: g.size.width / 2, y: g.size.height / 2)
                    }
                    .ignoresSafeArea()
                } else {
                    ContentView()
                }
            }
            .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        }
    }
}
