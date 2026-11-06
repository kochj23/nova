//
//  PhoneLayout.swift — Nova Cluster
//
//  iPhone (2026-11-06, Jordan: "how about an iPhone app?"): the iPad target is universal now. The phone gets the
//  same three views at a phone-sized layout unit, fewer card columns, and card grids that scroll instead of
//  pushing the lesson and the bottom bar off the screen.
//

import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

enum DeviceLayout {
    static let isPhone: Bool = {
        #if os(iOS)
        return UIDevice.current.userInterfaceIdiom == .phone
        #else
        return false
        #endif
    }()

    /// The layout unit off the Mac: an iPad's 820 pt short side is 1.0 (floor 0.8); an iPhone is a fixed 0.62.
    static func unit(_ size: CGSize) -> CGFloat {
        isPhone ? phoneUnit : max(0.8, min(size.width, size.height) / 820)
    }

    static let phoneUnit: CGFloat = 0.62

    /// Card columns: the iPad's counts, one fewer on a phone (never below one).
    static func columns(pad: Int) -> Int { isPhone ? max(1, pad - 1) : pad }

    /// On a phone the alert strip and "just published" ticker span the width, so the screens under them start
    /// lower (points; zero elsewhere, where the strips sit between the hero and the wordmark).
    static func stripPad(_ snap: ClusterSnapshot?) -> CGFloat {
        guard isPhone else { return 0 }
        return (snap?.published?.isEmpty == false ? 20 : 0) + (AlertText.headline(snap?.alerts) != nil ? 24 : 0)
    }
}

extension View {
    /// On a phone, cap `content` at `maxHeight` and let it scroll; elsewhere, unchanged.
    @ViewBuilder func phoneScroll(maxHeight: CGFloat) -> some View {
        if DeviceLayout.isPhone {
            ScrollView(.vertical, showsIndicators: false) { self }.frame(maxHeight: maxHeight)
        } else {
            self
        }
    }
}
