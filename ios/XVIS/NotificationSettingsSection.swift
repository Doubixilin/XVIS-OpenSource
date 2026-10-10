import SwiftUI
import UIKit

struct NotificationSettingsSection: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("任务提醒").font(.headline).foregroundStyle(XTheme.text)
            Toggle("结果与待处理提醒", isOn: Binding(
                get: { store.notificationsEnabled },
                set: { enabled in Task { await store.setNotificationsEnabled(enabled) } }
            ))
            .disabled(store.isUpdatingNotifications)
            Text(store.notificationPermission.label)
                .font(.subheadline).foregroundStyle(XTheme.secondary)
            if let note = store.notificationNote {
                Text(note).font(.subheadline).foregroundStyle(XTheme.secondary)
            }
            if store.notificationPermission == .denied {
                Button("打开系统通知设置") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                }.buttonStyle(XPressButtonStyle())
            } else if store.notificationsEnabled {
                Button("重试通知登记") { Task { await store.retryNotificationRegistration() } }
                    .buttonStyle(XPressButtonStyle())
                    .disabled(store.isUpdatingNotifications || !store.hasConnection)
            }
            Text("在任务完成、需要回答或批准时提醒。通知关闭或漏收时，仍可在 App 中查看结果与待处理项。")
                .font(.footnote).foregroundStyle(XTheme.secondary)
        }
        .padding(18)
        .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 22))
        .task {
            await store.refreshNotificationPermission()
            await store.refreshNotificationSettings()
        }
    }
}
