import Foundation
import Security

struct SavedInbox: Codable {
    var serverID: String
    var unreadCount: Int
    var items: [InboxItem]
    var nextCursor: String?
}

struct SavedState: Codable {
    var baseURL = ""
    var pending: PendingSubmission?
    var caches: [String: TaskCache] = [:]
    var newDraft = ""
    var taskDrafts: [String: String] = [:]
    var photoDrafts: [String: [PhotoDraft]] = [:]
    var assistantProfile: AssistantProfile?
    var inbox: SavedInbox?
    var notificationDeviceID = "d-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    var notificationsEnabled = false
    var projectContexts: [String: ProjectContext] = [:]
    var projectEditDrafts: [String: ProjectEditDraft] = [:]
    var interactionSubmissions: [String: InteractionSubmission] = [:]
    var controlSubmissions: [String: ControlCommand] = [:]
    var readingBookmarks: [String: String] = [:]

    init() { }
    private enum CodingKeys: String, CodingKey {
        case baseURL, pending, caches, newDraft, taskDrafts, photoDrafts, assistantProfile
        case inbox, notificationDeviceID, notificationsEnabled
        case projectContexts, projectEditDrafts, interactionSubmissions, controlSubmissions, readingBookmarks
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        assistantProfile = try values.decodeIfPresent(AssistantProfile.self, forKey: .assistantProfile)
        inbox = try values.decodeIfPresent(SavedInbox.self, forKey: .inbox)
        notificationDeviceID = try values.decodeIfPresent(String.self, forKey: .notificationDeviceID) ?? notificationDeviceID
        notificationsEnabled = try values.decodeIfPresent(Bool.self, forKey: .notificationsEnabled) ?? false
        projectContexts = try values.decodeIfPresent([String: ProjectContext].self, forKey: .projectContexts) ?? [:]
        projectEditDrafts = try values.decodeIfPresent([String: ProjectEditDraft].self, forKey: .projectEditDrafts) ?? [:]
        interactionSubmissions = try values.decodeIfPresent([String: InteractionSubmission].self, forKey: .interactionSubmissions) ?? [:]
        controlSubmissions = try values.decodeIfPresent([String: ControlCommand].self, forKey: .controlSubmissions) ?? [:]
        readingBookmarks = try values.decodeIfPresent([String: String].self, forKey: .readingBookmarks) ?? [:]
        baseURL = try values.decodeIfPresent(String.self, forKey: .baseURL) ?? ""
        pending = try values.decodeIfPresent(PendingSubmission.self, forKey: .pending)
        caches = try values.decodeIfPresent([String: TaskCache].self, forKey: .caches) ?? [:]
        newDraft = try values.decodeIfPresent(String.self, forKey: .newDraft) ?? ""
        taskDrafts = try values.decodeIfPresent([String: String].self, forKey: .taskDrafts) ?? [:]
        photoDrafts = try values.decodeIfPresent([String: [PhotoDraft]].self, forKey: .photoDrafts) ?? [:]
    }
}

final class StateDisk {
    let file: URL
    init(directory: URL? = nil) {
        let dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("XVIS", isDirectory: true)
        file = dir.appendingPathComponent("state.json")
    }
    func load() throws -> SavedState {
        guard FileManager.default.fileExists(atPath: file.path) else { return SavedState() }
        return try JSONDecoder().decode(SavedState.self, from: Data(contentsOf: file))
    }
    func save(_ state: SavedState) throws {
        do {
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONEncoder().encode(state).write(to: file, options: [.atomic, .completeFileProtection])
        } catch { throw AppFailure.localSave }
    }
}

struct ConnectionCredential: Codable, Equatable {
    let endpoint: String
    let token: String
}

protocol CredentialStore {
    func read() throws -> ConnectionCredential?
    func save(_ credential: ConnectionCredential) throws
    func clear() throws
}

struct KeychainCredentials: CredentialStore {
    let service: String
    init(service: String = (Bundle.main.bundleIdentifier ?? "org.example.xvis") + ".connection") { self.service = service }
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: "app-token"]
    }
    func read() throws -> ConnectionCredential? {
        var q = query; q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data,
              let credential = try? JSONDecoder().decode(ConnectionCredential.self, from: data) else { throw AppFailure.credentialSave }
        return credential
    }
    func save(_ credential: ConnectionCredential) throws {
        let data = try JSONEncoder().encode(credential)
        var q = query
        q[kSecValueData as String] = data
        q[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let added = SecItemAdd(q as CFDictionary, nil)
        if added == errSecDuplicateItem {
            let status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data,
                kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly] as CFDictionary)
            guard status == errSecSuccess else { throw AppFailure.credentialSave }
        } else if added != errSecSuccess { throw AppFailure.credentialSave }
    }
    func clear() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw AppFailure.credentialSave }
    }
}
