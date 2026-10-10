import SwiftUI

@main struct XVISApp: App {
    @UIApplicationDelegateAdaptor(NotificationAppDelegate.self) private var notificationDelegate
    @StateObject private var store = AppStore()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(store)
                .task { NotificationBridge.shared.attach(store) }
                .onChange(of: phase, initial: true) { _, phase in store.setForeground(phase == .active) }
        }
    }
}
