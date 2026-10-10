import Foundation
import UIKit
import UserNotifications

struct InboxItem: Codable, Identifiable, Equatable {
    var result_id: String
    var task_id: String
    var node_id: String
    var project_id: String
    var title: String
    var status: String
    var revision: Int
    var created_at: String
    var read_at: String?
    var id: String { result_id }
    var isValid: Bool {
        XNotificationID.matches(result_id, prefix: "r") &&
        XNotificationID.safeTask(task_id) && revision > 0 &&
        ["done", "failed", "cancelled", "timed_out", "interrupted"].contains(status)
    }
}

struct InboxResponse: Decodable {
    var server_id: String
    var unread_count: Int
    var items: [InboxItem]
    var next_cursor: String?
}

struct InboxItemResponse: Decodable {
    var server_id: String
    var item: InboxItem
}

struct NotificationSettings: Decodable {
    var server_id: String
    var push_available: Bool
}

struct NotificationDeviceResponse: Decodable {
    var server_id: String
    var device_id: String
    var enabled: Bool
    var push_available: Bool
}

struct NotificationRoute: Equatable {
    var taskID: String
    var resultID: String
    var revision: Int
    var serverID: String
    var interactionID: String? = nil
}

struct XNotificationPayload: Codable, Equatable {
    var server_id: String
    var task_id: String
    var result_id: String = ""
    var revision: Int
    var interaction_id: String? = nil
    private enum CodingKeys: String, CodingKey { case server_id, task_id, result_id, revision, interaction_id }
    init(server_id: String, task_id: String, result_id: String = "", revision: Int, interaction_id: String? = nil) {
        self.server_id = server_id; self.task_id = task_id; self.result_id = result_id
        self.revision = revision; self.interaction_id = interaction_id
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        server_id = try values.decode(String.self, forKey: .server_id)
        task_id = try values.decode(String.self, forKey: .task_id)
        result_id = try values.decodeIfPresent(String.self, forKey: .result_id) ?? ""
        revision = try values.decode(Int.self, forKey: .revision)
        interaction_id = try values.decodeIfPresent(String.self, forKey: .interaction_id)
    }
    var isValid: Bool {
        XNotificationID.matches(server_id, prefix: "s") &&
        (interaction_id == nil ? XNotificationID.matches(result_id, prefix: "r") : result_id.isEmpty && XNotificationID.matches(interaction_id ?? "", prefix: "q") && revision == 1) &&
        XNotificationID.safeTask(task_id) && revision > 0
    }
    static func parse(_ info: [AnyHashable: Any]) -> XNotificationPayload? {
        guard let value = info["xvis"] as? [String: Any],
              JSONSerialization.isValidJSONObject(value),
              let bytes = try? JSONSerialization.data(withJSONObject: value),
              let payload = try? JSONDecoder().decode(Self.self, from: bytes),
              payload.isValid else { return nil }
        return payload
    }
}

enum XNotificationID {
    static func matches(_ value: String, prefix: String) -> Bool {
        value.range(of: "^\(prefix)-[0-9a-f]{32}$", options: .regularExpression) != nil
    }
    static func safeTask(_ value: String) -> Bool {
        value.utf8.count <= 128 && value.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil
    }
    static func validToken(_ value: String) -> Bool {
        (32...512).contains(value.count) && value.count.isMultiple(of: 2) &&
        value.range(of: "^[0-9a-f]+$", options: .regularExpression) != nil
    }
}

enum NotificationPermission: String {
    case unknown, notDetermined, denied, authorized, provisional, ephemeral
    var allowed: Bool { [.authorized, .provisional, .ephemeral].contains(self) }
    var label: String {
        switch self {
        case .unknown: return "正在检查系统权限"
        case .notDetermined: return "尚未请求系统权限"
        case .denied: return "系统通知已关闭"
        case .authorized: return "系统通知已允许"
        case .provisional: return "系统允许安静通知"
        case .ephemeral: return "系统临时允许通知"
        }
    }
}

@MainActor protocol NotificationSystem {
    func permission() async -> NotificationPermission
    func requestPermission() async throws -> NotificationPermission
    func register()
}

@MainActor final class SystemNotifications: NotificationSystem {
    func permission() async -> NotificationPermission {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .notDetermined: return .notDetermined
        case .denied: return .denied
        case .authorized: return .authorized
        case .provisional: return .provisional
        case .ephemeral: return .ephemeral
        @unknown default: return .unknown
        }
    }
    func requestPermission() async throws -> NotificationPermission {
        _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        return await permission()
    }
    func register() { UIApplication.shared.registerForRemoteNotifications() }
}

// Tokens remain in memory. APNs supplies the current token on every launch.
@MainActor final class NotificationBridge {
    static let shared = NotificationBridge()
    private weak var store: AppStore?
    private var pendingTap: XNotificationPayload?
    private var token: String?
    func attach(_ store: AppStore) {
        self.store = store
        if let token { Task { await store.didRegisterNotificationToken(token) } }
        if let pendingTap {
            self.pendingTap = nil
            Task { await store.handleNotification(pendingTap) }
        }
    }
    func receivedToken(_ token: String) {
        self.token = token
        if let store { Task { await store.didRegisterNotificationToken(token) } }
    }
    func registrationFailed() { store?.notificationRegistrationFailed() }
    func tapped(_ payload: XNotificationPayload) {
        if let store { Task { await store.handleNotification(payload) } }
        else { pendingTap = payload }
    }
    func receivedInForeground() {
        if let store { Task { await store.refreshInbox(automatically: true) } }
    }
}

final class NotificationAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in NotificationBridge.shared.receivedToken(token) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in NotificationBridge.shared.registrationFailed() }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in NotificationBridge.shared.receivedInForeground() }
        completionHandler([])
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier,
           let payload = XNotificationPayload.parse(response.notification.request.content.userInfo) {
            Task { @MainActor in NotificationBridge.shared.tapped(payload) }
        }
        completionHandler()
    }
}
