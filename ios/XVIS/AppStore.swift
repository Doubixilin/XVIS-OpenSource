import Foundation
import Combine

@MainActor final class AppStore: ObservableObject {
    @Published private(set) var state = SavedState()
    @Published private(set) var nodes: [NodeView] = []
    @Published private(set) var projects: [ProjectView] = []
    @Published private(set) var tasks: [TaskView] = []
    @Published private(set) var nextCursor: String?
    @Published private(set) var isSending = false
    @Published private(set) var isRefreshing = false
    @Published private(set) var isLoadingMore = false
    @Published private(set) var hasConnection = false
    @Published private(set) var connectionFailed = false
    @Published var errorMessage: String?
    @Published var connectionNote: String?
    @Published var submittedTaskID: String?
    @Published private(set) var lastConfirmedSubmission: PendingSubmission?
    @Published private(set) var conversationLoading = false
    @Published private(set) var conversationNote: String?
    @Published private(set) var pendingInteractions: [TaskInteraction] = []
    @Published private(set) var inboxItems: [InboxItem] = []
    @Published private(set) var unreadCount = 0
    @Published private(set) var inboxNextCursor: String?
    @Published private(set) var isRefreshingInbox = false
    @Published private(set) var isLoadingMoreInbox = false
    @Published private(set) var inboxNote: String?
    @Published private(set) var notificationRoute: NotificationRoute?
    var requestedInteractionID: String?
    @Published private(set) var notificationPermission: NotificationPermission = .unknown
    @Published private(set) var pushAvailable: Bool?
    @Published private(set) var notificationRegistered = false
    @Published private(set) var isUpdatingNotifications = false
    @Published private(set) var notificationNote: String?
    var notificationsEnabled: Bool { state.notificationsEnabled }
    var activeTaskID: String? {
        didSet {
            if activeTaskID != oldValue {
                stopEventStream()
                conversationLoadVersion += 1
                conversationLoading = false
                conversationNote = nil
                confirmedDisplayedTaskID = nil
                lookedUpDisplayedTaskID = nil
            }
        }
    }
    private let disk: StateDisk
    private let imageDisk: ImageDisk
    private let fileDisk: FileDisk
    private let credentials: CredentialStore
    private let clientFactory: (URL, String) -> APIClient
    private var credential: ConnectionCredential?
    private var polling: Task<Void, Never>?
    private var eventStream: Task<Void, Never>?
    private var streamedTaskID: String?
    private var foreground = false
    private var detailLoads: [String: Task<Void, Error>] = [:]
    private var conversationLoadVersion = 0
    private var generation = 0
    private let notificationSystem: NotificationSystem
    private var notificationToken: String?
    private var registeredNotificationToken: String?
    private var serverID: String?
    private var connectionChanging = false
    private var inboxRequestVersion = 0
    private var inboxMutationVersion = 0
    private var hasLoadedOlderInboxPages = false
    private var notificationRouteVersion = 0
    private var pendingNotification: XNotificationPayload?
    private var notificationWork: Task<Void, Never>?
    private var notificationRemoval: Task<Void, Never>?
    private var notificationDeviceWork: Task<Void, Never>?
    private var notificationWriteTail: Task<Void, Never>?
    private var notificationDeviceSyncRequested = false
    private var knownInboxResults: [String: InboxItem] = [:]
    private var displayedInboxResults: [String: InboxItem] = [:]
    private var inboxReads: Set<String> = []
    private var confirmedDisplayedTaskID: String?
    private var lookedUpDisplayedTaskID: String?
    static let conversationLimit = 20

    var isPolling: Bool { foreground && polling != nil && hasConnection }

    init(disk: StateDisk = StateDisk(), credentials: CredentialStore = KeychainCredentials(),
         clientFactory: @escaping (URL, String) -> APIClient = { APIClient(baseURL: $0, token: $1) },
         notificationSystem: NotificationSystem? = nil) {
        self.disk = disk
        self.imageDisk = ImageDisk(directory: disk.file.deletingLastPathComponent())
        self.fileDisk = FileDisk(directory: disk.file.deletingLastPathComponent())
        self.credentials = credentials; self.clientFactory = clientFactory
        self.notificationSystem = notificationSystem ?? SystemNotifications()
        do {
            state = try disk.load(); credential = try credentials.read()
            if let saved = state.inbox, XNotificationID.matches(saved.serverID, prefix: "s") {
                serverID = saved.serverID
                inboxItems = saved.items.filter { $0.isValid && $0.read_at == nil }
                hasLoadedOlderInboxPages = inboxItems.count > 30
                unreadCount = max(0, saved.unreadCount); inboxNextCursor = saved.nextCursor
                knownInboxResults = Dictionary(inboxItems.map { ($0.task_id, $0) }, uniquingKeysWith: { _, latest in latest })
            }
            if let credential {
                guard credential.endpoint == (try APIClient.validatedURL(state.baseURL)).absoluteString else { throw AppFailure.connectionMismatch }
                hasConnection = !credential.token.isEmpty
            }
        }
        catch AppFailure.connectionMismatch { errorMessage = AppFailure.connectionMismatch.localizedDescription }
        catch { errorMessage = "无法读取手机保存的连接或记录，请检查后重新设置；原文件已保留。" }
    }

    private func client() throws -> APIClient {
        guard let credential, !credential.token.isEmpty else { throw AppFailure.noConnection }
        let url = try APIClient.validatedURL(state.baseURL)
        guard credential.endpoint == url.absoluteString else { throw AppFailure.connectionMismatch }
        return clientFactory(url, credential.token)
    }

    private func persist(_ next: SavedState) throws {
        try disk.save(next)
        state = next
    }

    func submitControl(taskID: String, command: ControlCommand) async throws {
        let expectedGeneration = generation
        let frozen = state.controlSubmissions[taskID] ?? command
        var next = state; next.controlSubmissions[taskID] = frozen
        try persist(next)
        let api = try client()
        let receipt = try await api.submitControl(taskID: taskID, command: frozen)
        guard generation == expectedGeneration else { throw CancellationError() }
        guard receipt.task_id == taskID, receipt.command_id == frozen.id, receipt.epoch == frozen.epoch else { throw AppFailure.protocolMismatch }
        next = state; next.controlSubmissions.removeValue(forKey: taskID)
        try persist(next)
        try await refreshDetail(taskID, api: api)
    }
    func discardControl(taskID: String) {
        var next = state; next.controlSubmissions.removeValue(forKey: taskID)
        do { try persist(next) } catch { show(error) }
    }
    func saveReadingBookmark(_ anchor: String, conversationID: String) {
        guard state.readingBookmarks[conversationID] != anchor else { return }
        var next = state; next.readingBookmarks[conversationID] = anchor
        if next.readingBookmarks.count > 100 {
            for key in next.readingBookmarks.keys.sorted() where key != conversationID {
                if next.readingBookmarks.count <= 100 { break }
                next.readingBookmarks.removeValue(forKey: key)
            }
        }
        do { try persist(next) } catch { show(error) }
    }
    func inspectPairing(_ text: String) async throws -> PairingPreview {
        let expected = generation, api = try client()
        let link = try PairingLink(text, expectedServer: api.baseURL)
        let value: PairingPreview = try await api.post("api/v1/pairing/\(link.requestID)/inspect", body: ["secret": link.secret])
        guard generation == expected else { throw CancellationError() }
        guard value.isValid, value.request_id == link.requestID else { throw AppFailure.protocolMismatch }
        return value
    }
    func approvePairing(_ text: String, expected: PairingPreview) async throws -> PairingPreview {
        let version = generation, api = try client()
        let link = try PairingLink(text, expectedServer: api.baseURL)
        guard expected.isValid, link.requestID == expected.request_id else { throw AppFailure.protocolMismatch }
        let value: PairingPreview = try await api.post("api/v1/pairing/\(link.requestID)/approve", body: ["secret": link.secret])
        guard generation == version else { throw CancellationError() }
        guard value.isValid, value.status == "approved", value.node_id != nil,
              value.request_id == expected.request_id, value.name == expected.name,
              value.fingerprint == expected.fingerprint else { throw AppFailure.protocolMismatch }
        await refreshVisible()
        return value
    }
    func pairedNodes() async throws -> [PairedNode] {
        let version = generation
        let response: PairedNodes = try await client().get("api/v1/paired-nodes")
        guard version == generation else { throw CancellationError() }
        guard response.nodes.allSatisfy({ XNotificationID.matches($0.id, prefix: "n") }) else { throw AppFailure.protocolMismatch }
        return response.nodes
    }
    func revokePairing(_ nodeID: String) async throws {
        guard XNotificationID.matches(nodeID, prefix: "n") else { throw AppFailure.protocolMismatch }
        let version = generation
        let value: PairingRevoked = try await client().post("api/v1/paired-nodes/\(nodeID)/revoke", body: [:])
        guard generation == version else { throw CancellationError() }
        guard value.node_id == nodeID, value.revoked else { throw AppFailure.protocolMismatch }
        await refreshVisible()
    }
    private func stopEventStream() {
        eventStream?.cancel(); eventStream = nil; streamedTaskID = nil
    }
    private func startEventStream(_ id: String) {
        guard foreground, hasConnection, state.caches[id]?.snapshot?.terminal == false,
              state.caches[id]?.snapshot?.control != nil else {
            stopEventStream(); return
        }
        if streamedTaskID == id { return }
        stopEventStream(); streamedTaskID = id
        let expectedGeneration = generation
        eventStream = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled && self.foreground && self.generation == expectedGeneration {
                do {
                    let api = try self.client()
                    try await api.streamEvents(taskID: id, after: self.state.caches[id]?.after ?? 0) { page in
                        guard !Task.isCancelled, self.generation == expectedGeneration else { throw CancellationError() }
                        try self.consumeEvents(page, taskID: id)
                    }
                } catch is CancellationError { break }
                catch { // Polling remains available while a stream reconnects or an older Hub rejects it.
                    do { try await Task.sleep(for: .seconds(3)) } catch { break }
                }
            }
        }
    }
    private func consumeEvents(_ page: EventsResponse, taskID: String) throws {
        let cache = state.caches[taskID] ?? TaskCache()
        guard page.events.allSatisfy({ $0.task_id == taskID }),
              page.next_after == (page.events.last?.id ?? page.next_after) else { throw AppFailure.protocolMismatch }
        if page.next_after <= cache.after { return }
        let fresh = EventsResponse(events: page.events.filter { $0.id > cache.after }, next_after: page.next_after, has_more: page.has_more)
        var next = state; next.caches[taskID] = try cache.consuming(fresh, taskID: taskID)
        try persist(next)
    }

    func respondToInteraction(_ item: TaskInteraction, value: JSONValue) async throws {
        guard item.canRespond, hasConnection else { throw AppFailure.network("此待处理项已失效，请刷新状态。") }
        let expectedGeneration = generation
        let submission = state.interactionSubmissions[item.id] ?? InteractionSubmission(revision: item.revision, value: value)
        var next = state
        next.interactionSubmissions[item.id] = submission
        try persist(next)
        let api = try client()
        do {
            let updated = try await api.respondToInteraction(item, submission: submission)
            guard expectedGeneration == generation else { throw CancellationError() }
            guard updated.isValid, updated.id == item.id, updated.task_id == item.task_id,
                  updated.response_id == submission.response_id else { throw AppFailure.protocolMismatch }
            next = state; next.interactionSubmissions.removeValue(forKey: item.id)
            try persist(next)
            try await refreshDetail(item.task_id, api: api)
            await refreshVisible()
        } catch {
            if expectedGeneration == generation { try? await refreshDetail(item.task_id, api: api) }
            throw error
        }
    }

    func draft(for taskID: String?) -> String {
        taskID.map { state.taskDrafts[$0] ?? "" } ?? state.newDraft
    }

    func setDraft(_ text: String, for taskID: String?) {
        guard text != draft(for: taskID) else { return }
        var next = state
        if let taskID {
            if text.isEmpty { next.taskDrafts.removeValue(forKey: taskID) }
            else { next.taskDrafts[taskID] = text }
        } else { next.newDraft = text }
        do { try persist(next) } catch { show(error) }
    }

    func imageDrafts(for taskID: String?) -> [PhotoDraft] { state.photoDrafts[taskID ?? "new"] ?? [] }

    func addImage(data: Data, for taskID: String?) throws {
        guard imageDrafts(for: taskID).count < 4 else { throw AppFailure.network("每条消息最多添加四张图片。") }
        let draft = try imageDisk.draft(data)
        var next = state
        next.photoDrafts[taskID ?? "new", default: []].append(draft)
        try persist(next)
    }

    func removeImage(id: String, for taskID: String?) {
        var next = state
        let key = taskID ?? "new"
        next.photoDrafts[key]?.removeAll { $0.id == id }
        if next.photoDrafts[key]?.isEmpty == true { next.photoDrafts.removeValue(forKey: key) }
        do { try persist(next) } catch { show(error) }
    }

    func imageData(for draft: PhotoDraft) throws -> Data { try imageDisk.read(draft.fileName) }

    func loadImage(_ image: ImageView, taskID: String?) async throws -> Data {
        let name = try ImageDisk.cachedName(image)
        if let cached = try? imageDisk.read(name), (try? ImageDisk.verify(cached, against: image)) != nil { return cached }
        let expectedGeneration = generation
        let data = try await client().imageData(image, taskID: taskID)
        try Task.checkCancellation()
        guard expectedGeneration == generation else { throw CancellationError() }
        try ImageDisk.verify(data, against: image)
        try imageDisk.write(data, named: name)
        return data
    }

    static let imageOnlyPrompt = "请处理这些图片。"

    private func clearConfirmedDraft(_ submission: PendingSubmission, createdTaskID: String, in next: inout SavedState) {
        let key = submission.followsTaskID ?? "new"
        let text = submission.followsTaskID.map { next.taskDrafts[$0] ?? "" } ?? next.newDraft
        let sameText = text == submission.content || (!submission.imageIDs.isEmpty && submission.content == Self.imageOnlyPrompt && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        let sameImages = (next.photoDrafts[key] ?? []) == (submission.images ?? [])
        if let parent = submission.followsTaskID {
            if sameText && sameImages {
                next.taskDrafts.removeValue(forKey: parent); next.photoDrafts.removeValue(forKey: key)
            } else if (!text.isEmpty || !(next.photoDrafts[key] ?? []).isEmpty),
                      next.taskDrafts[createdTaskID]?.isEmpty != false,
                      next.photoDrafts[createdTaskID]?.isEmpty != false {
                // Composer follows the accepted child; preserve edits made while its request was pending.
                next.taskDrafts[createdTaskID] = text
                next.photoDrafts[createdTaskID] = next.photoDrafts[key]
                next.taskDrafts.removeValue(forKey: parent)
                next.photoDrafts.removeValue(forKey: key)
            }
        } else if sameText && sameImages { next.newDraft = ""; next.photoDrafts.removeValue(forKey: key) }
    }

    // A bounded contiguous window around the selected round, prioritizing later rounds.
    func conversationTaskIDs(for taskID: String) -> [String] {
        var ids = [taskID]
        var visited: Set<String> = [taskID]
        let origin = state.caches[taskID]?.snapshot
        while ids.count < Self.conversationLimit,
              let current = ids.last, let following = state.caches[current]?.snapshot?.followup_task_id,
              !visited.contains(following), let task = state.caches[following]?.snapshot,
              task.is_followup_of == current, sameWorkspace(task, origin) {
            ids.append(following); visited.insert(following)
        }
        while ids.count < Self.conversationLimit,
              let current = ids.first, let previous = state.caches[current]?.snapshot?.is_followup_of,
              !visited.contains(previous), let task = state.caches[previous]?.snapshot,
              task.followup_task_id == current, sameWorkspace(task, origin) {
            ids.insert(previous, at: 0); visited.insert(previous)
        }
        return ids
    }

    private func sameWorkspace(_ task: TaskView, _ origin: TaskView?) -> Bool {
        guard let origin else { return false }
        return task.project_id == origin.project_id && task.node_id == origin.node_id
    }

    func loadConversation(containing taskID: String) async {
        activeTaskID = taskID
        conversationLoadVersion += 1
        let version = conversationLoadVersion
        let expectedGeneration = generation
        conversationLoading = true
        conversationNote = nil
        defer { if version == conversationLoadVersion { conversationLoading = false } }
        do {
            let api = try client()
            try await refreshDetail(taskID, api: api)
            try Task.checkCancellation()
            guard expectedGeneration == generation, version == conversationLoadVersion,
                  let origin = state.caches[taskID]?.snapshot else { return }
            var ids = [taskID]
            var visited: Set<String> = [taskID]
            var note: String?
            // Follow existing cached links as well as remote links; both consume the same limit.
            for forwards in [true, false] {
                while ids.count < Self.conversationLimit {
                    let current = forwards ? ids.last! : ids.first!
                    guard let snapshot = state.caches[current]?.snapshot else { break }
                    guard let linked = forwards ? snapshot.followup_task_id : snapshot.is_followup_of else { break }
                    guard !visited.contains(linked) else {
                        note = "会话关联形成循环，已保留可读取的记录。可刷新后重试。"
                        break
                    }
                    do {
                        // Cached content remains visible while links and any incomplete event page are recovered.
                        try await refreshDetail(linked, api: api)
                        try Task.checkCancellation()
                        guard expectedGeneration == generation, version == conversationLoadVersion else { return }
                        guard let task = state.caches[linked]?.snapshot, sameWorkspace(task, origin),
                              (forwards ? task.is_followup_of == current : task.followup_task_id == current) else {
                            throw AppFailure.protocolMismatch
                        }
                        visited.insert(linked)
                        if forwards { ids.append(linked) } else { ids.insert(linked, at: 0) }
                    } catch is CancellationError { throw CancellationError() }
                    catch {
                        note = "部分会话记录暂未读取，已保留现有内容。可刷新重试。"
                        break
                    }
                }
            }
            if ids.count == Self.conversationLimit,
               (state.caches[ids.first!]?.snapshot?.is_followup_of != nil || state.caches[ids.last!]?.snapshot?.followup_task_id != nil) {
                note = "当前显示最多 20 轮。可从历史记录选择更早或较新的轮次。"
            }
            guard expectedGeneration == generation, version == conversationLoadVersion else { return }
            conversationNote = note
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation, version == conversationLoadVersion else { return }
            conversationNote = "暂时无法补齐会话，已保留手机记录。可刷新重试。"
            if case AppFailure.network = error { connectionFailed = true }
            show(error)
        }
    }

    func connect(baseURL: String, newToken: String) async {
        guard !connectionChanging else { return }
        connectionChanging = true
        defer { connectionChanging = false }
        do {
            let url = try APIClient.validatedURL(baseURL)
            let supplied = newToken.trimmingCharacters(in: .whitespacesAndNewlines)
            let currentURL = try? APIClient.validatedURL(state.baseURL)
            guard !isSending else { throw AppFailure.pendingExists }
            if currentURL != url && state.pending != nil { throw AppFailure.pendingExists }
            guard !supplied.isEmpty || (currentURL == url && credential?.endpoint == url.absoluteString && !(credential?.token.isEmpty ?? true)) else { throw AppFailure.noConnection }
            let chosen = supplied.isEmpty ? credential!.token : supplied
            guard !chosen.contains("\n"), !chosen.contains("\r") else { throw AppFailure.credentialSave }
            // Authentication is tested before replacing a working saved connection.
            let api = clientFactory(url, chosen)
            let expectedGeneration = generation
            let _: NodesResponse = try await api.get("api/v1/nodes")
            guard expectedGeneration == generation, !isSending else { throw AppFailure.pendingExists }
            if currentURL != url && state.pending != nil { throw AppFailure.pendingExists }
            polling?.cancel()
            detailLoads.values.forEach { $0.cancel() }
            conversationLoadVersion += 1; conversationLoading = false; conversationNote = nil
            var next = state
            if currentURL != url {
                next.caches = [:]
                next.newDraft = ""; next.taskDrafts = [:]
                next.photoDrafts = [:]
                next.assistantProfile = nil
                next.projectContexts = [:]; next.projectEditDrafts = [:]; next.interactionSubmissions = [:]; next.controlSubmissions = [:]; next.readingBookmarks = [:]
                next.inbox = nil; next.notificationsEnabled = false
            }
            next.baseURL = url.absoluteString
            let replacement = ConnectionCredential(endpoint: url.absoluteString, token: chosen)
            let oldAPI = try? client()
            let oldDeviceID = state.notificationDeviceID
            try credentials.save(replacement)
            do { try persist(next) }
            catch {
                if let credential { try? credentials.save(credential) } else { try? credentials.clear() }
                throw error
            }
            generation += 1
            stopEventStream()
            resetNotificationConnection()
            pendingInteractions = []
            if currentURL != url {
                if let oldAPI { notificationRemoval = Task { _ = try? await self.orderedNotificationWrite { try await oldAPI.deleteNotificationDevice(id: oldDeviceID) } } }
            }
            if currentURL != url { tasks = []; nodes = []; projects = []; nextCursor = nil; activeTaskID = nil }
            credential = replacement; hasConnection = true; errorMessage = nil; connectionNote = "连接成功，访问凭证已安全保存。"
            if foreground { startPolling() }
            refreshNotificationStateSoon()
        } catch { show(error); if foreground && hasConnection { startPolling() } }
    }

    func clearConnection() {
        do {
            guard !isSending else { throw AppFailure.pendingExists }
            polling?.cancel()
            detailLoads.values.forEach { $0.cancel() }
            conversationLoadVersion += 1; conversationLoading = false; conversationNote = nil
            try credentials.clear()
            let oldAPI = try? client()
            let deviceID = state.notificationDeviceID
            var next = SavedState(); next.baseURL = state.baseURL; next.notificationDeviceID = deviceID
            do { try persist(next) }
            catch { if let credential { try? credentials.save(credential) }; throw error }
            generation += 1
            stopEventStream()
            resetNotificationConnection()
            pendingInteractions = []
            credential = nil; hasConnection = false
            nodes = []; projects = []; tasks = []; nextCursor = nil; activeTaskID = nil
            connectionNote = "已清除手机连接。服务器上的任务继续按原状态运行。"
            if let oldAPI { notificationRemoval = Task { _ = try? await self.orderedNotificationWrite { try await oldAPI.deleteNotificationDevice(id: deviceID) } } }
        } catch { show(error); if foreground && hasConnection { startPolling() } }
    }

    func setForeground(_ active: Bool) {
        foreground = active
        polling?.cancel()
        if !active { stopEventStream() }
        if !active { detailLoads.values.forEach { $0.cancel() }; notificationWork?.cancel() }
        if active && hasConnection {
            startPolling()
            refreshNotificationStateSoon()
            if let pendingNotification {
                self.pendingNotification = nil
                Task { await handleNotification(pendingNotification) }
            }
        }
    }
    private func startPolling() {
        polling?.cancel()
        polling = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshVisible()
                do { try await Task.sleep(for: .seconds(2)) } catch { break }
            }
        }
    }

    func refreshVisible() async {
        guard hasConnection, !isRefreshing, !isLoadingMore else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let api = try client()
            let expectedGeneration = generation
            let nr: NodesResponse = try await api.get("api/v1/nodes")
            try Task.checkCancellation()
            let pr: ProjectsResponse = try await api.get("api/v1/projects")
            try Task.checkCancellation()
            guard expectedGeneration == generation else { return }
            nodes = nr.nodes; projects = pr.projects
            let list: TasksResponse = try await api.get("api/v1/tasks", query: [URLQueryItem(name: "limit", value: "30")])
            try Task.checkCancellation()
            guard expectedGeneration == generation else { return }
            // Keep already loaded older pages when the first page is refreshed.
            mergeTasks(list.tasks)
            nextCursor = list.next_cursor
            if let id = activeTaskID {
                let ids = conversationTaskIDs(for: id)
                let latest = ids.last ?? id
                try await refreshDetail(latest, api: api)
                startEventStream(latest)
                guard activeTaskID == id, expectedGeneration == generation else { return }
                if ids.count < Self.conversationLimit,
                   let following = state.caches[latest]?.snapshot?.followup_task_id, !ids.contains(following) {
                    await loadConversation(containing: id)
                }
            }
            connectionNote = nil
            connectionFailed = false
            if projects.contains(where: { $0.execution_mode != nil }) {
                let pending: InteractionsResponse = try await api.get("api/v1/interactions")
                guard expectedGeneration == generation else { return }
                guard pending.items.allSatisfy(\.isValid) else { throw AppFailure.protocolMismatch }
                pendingInteractions = pending.items
            }
            await refreshInbox(automatically: true)
        } catch is CancellationError { }
        catch { connectionNote = error.localizedDescription; connectionFailed = true }
    }

    func loadMoreTasks() async {
        guard var cursor = nextCursor, !isLoadingMore, !isRefreshing else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let expectedGeneration = generation
            let api = try client()
            var visited: Set<String> = []
            while true {
                guard visited.insert(cursor).inserted else { throw AppFailure.protocolMismatch }
                let page: TasksResponse = try await api.get("api/v1/tasks", query: [URLQueryItem(name: "cursor", value: cursor), URLQueryItem(name: "limit", value: "30")])
                try Task.checkCancellation()
                guard expectedGeneration == generation else { return }
                let knownIDs = Set(tasks.map(\.task_id))
                let hasNewTasks = page.tasks.contains { !knownIDs.contains($0.task_id) }
                mergeTasks(page.tasks)
                nextCursor = page.next_cursor
                // A foreground refresh resets the server cursor. Skip cached pages
                // within this request so older uncached pages remain reachable.
                guard !hasNewTasks, let following = page.next_cursor else { break }
                cursor = following
            }
        } catch { show(error) }
    }

    private func mergeTasks(_ incoming: [TaskView]) {
        var byID = Dictionary(uniqueKeysWithValues: tasks.map { ($0.task_id, $0) })
        for var task in incoming {
            // List responses omit the original instruction supplied by details.
            if task.prompt == nil { task.prompt = byID[task.task_id]?.prompt }
            byID[task.task_id] = task
        }
        tasks = byID.values.sorted {
            $0.created_at == $1.created_at ? $0.task_id > $1.task_id : $0.created_at > $1.created_at
        }
    }

    func openDetail(_ id: String) async {
        activeTaskID = id
        do { try await refreshDetail(id, api: client()) }
        catch is CancellationError { }
        catch {
            if case AppFailure.network = error { connectionFailed = true }
            show(error)
        }
    }
    private func refreshDetail(_ id: String, api: APIClient) async throws {
        let expectedGeneration = generation
        let key = "\(expectedGeneration):\(id)"
        if let existing = detailLoads[key] { try await existing.value; try Task.checkCancellation(); return }
        let loading = Task { try await self.fetchDetail(id, api: api, expectedGeneration: expectedGeneration) }
        detailLoads[key] = loading
        defer { detailLoads.removeValue(forKey: key) }
        try await loading.value
        try Task.checkCancellation()
    }

    private func fetchDetail(_ id: String, api: APIClient, expectedGeneration: Int) async throws {
        try Task.checkCancellation()
        guard expectedGeneration == generation else { return }
        // Always refresh the task snapshot before catching up its event stream.
        let task: TaskView = try await api.get("api/v1/tasks/\(id)")
        try Task.checkCancellation()
        guard task.id == id else { throw AppFailure.protocolMismatch }
        if let context = task.project_context, context.schema_version != nil || context.revision > 0 {
            guard context.schema_version == 1, context.node_id == task.node_id, context.project_id == task.project_id,
                  context.validationMessage == nil,
                  task.project_context_revision == nil || task.project_context_revision == context.revision else { throw AppFailure.protocolMismatch }
        }
        if let files = task.output_files {
            guard files.count <= 4, files.allSatisfy(\.isValid), Set(files.map(\.id)).count == files.count else { throw AppFailure.protocolMismatch }
        }
        if let items = task.interactions {
            guard items.allSatisfy({ $0.isValid && $0.task_id == id }), Set(items.map(\.id)).count == items.count else { throw AppFailure.protocolMismatch }
        }
        if let control = task.control, !control.isValid { throw AppFailure.protocolMismatch }
        guard expectedGeneration == generation else { return }
        var next = state
        for item in task.interactions ?? [] where item.status != "pending" {
            next.interactionSubmissions.removeValue(forKey: item.id)
        }
        if let saved = next.controlSubmissions[id], (task.control?.commands ?? []).contains(where: { $0.id == saved.id }) {
            next.controlSubmissions.removeValue(forKey: id)
        }
        var cache = next.caches[id] ?? TaskCache()
        cache.applySnapshot(task); next.caches[id] = cache
        try persist(next)
        if let index = tasks.firstIndex(where: { $0.task_id == id }) { tasks[index] = task }
        repeat {
            let page: EventsResponse = try await api.get("api/v1/tasks/\(id)/events", query: [URLQueryItem(name: "after", value: String(cache.after)), URLQueryItem(name: "limit", value: "100")])
            try Task.checkCancellation()
            guard expectedGeneration == generation else { return }
            try consumeEvents(page, taskID: id)
            cache = state.caches[id] ?? cache
            if !page.has_more { break }
        } while !Task.isCancelled
        if expectedGeneration == generation, let item = knownInboxResults[id],
           let snapshot = state.caches[id]?.snapshot, snapshot.terminal, snapshot.status == item.status,
           snapshot.node_id == item.node_id, snapshot.project_id == item.project_id {
            displayedInboxResults[id] = item
        }
    }

    @discardableResult
    func submit(nodeID: String, projectID: String, prompt: String, images: [PhotoDraft]? = nil) async -> PendingSubmission? {
        do {
            guard state.pending == nil else { throw AppFailure.pendingExists }
            let frozen = images ?? imageDrafts(for: nil)
            let content = prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !frozen.isEmpty ? Self.imageOnlyPrompt : prompt
            try validate(content)
            var next = state; next.pending = .newTask(nodeID: nodeID, projectID: projectID, prompt: content, images: frozen)
            try persist(next)
            return await retryPending()
        } catch { show(error); return nil }
    }
    @discardableResult
    func followup(taskID: String, message: String) async -> PendingSubmission? {
        do {
            guard state.pending == nil else { throw AppFailure.pendingExists }
            let frozen = imageDrafts(for: taskID)
            let content = message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !frozen.isEmpty ? Self.imageOnlyPrompt : message
            try validate(content)
            var next = state; next.pending = .followup(taskID: taskID, message: content, images: frozen)
            try persist(next)
            return await retryPending()
        } catch { show(error); return nil }
    }
    @discardableResult
    func retryPending() async -> PendingSubmission? {
        guard let pending = state.pending, !isSending else { return nil }
        isSending = true
        defer { isSending = false }
        do {
            let expectedGeneration = generation
            let api = try client()
            for image in pending.images ?? [] {
                let data = try imageData(for: image)
                _ = try await api.uploadImage(id: image.imageID, data: data)
                try Task.checkCancellation()
                guard expectedGeneration == generation, state.pending == pending else { throw CancellationError() }
            }
            let path = pending.followsTaskID.map { "api/v1/tasks/\($0)/followup" } ?? "api/v1/tasks"
            let response: SubmissionResponse = try await api.post(path, body: pending.body, imageIDs: pending.imageIDs)
            guard expectedGeneration == generation, state.pending == pending else { throw CancellationError() }
            var next = state; next.pending = nil
            clearConfirmedDraft(pending, createdTaskID: response.task_id, in: &next)
            try persist(next)
            submittedTaskID = response.task_id
            lastConfirmedSubmission = pending
            if response.node_online == false { connectionNote = "任务已保存，等待执行设备上线。" }
            await refreshVisible()
            return pending
        } catch {
            if case AppFailure.network = error { connectionFailed = true }
            show(error); return nil
        }
    }
    func retryTaskWithImages(_ task: TaskView) async {
        guard let prompt = task.prompt, state.pending == nil, !isSending else { return }
        do {
            let expectedGeneration = generation
            var images: [PhotoDraft] = []
            for image in (task.images ?? []).filter({ $0.direction == "input" }) {
                _ = try await loadImage(image, taskID: task.task_id)
                guard expectedGeneration == generation else { throw CancellationError() }
                images.append(PhotoDraft(imageID: image.image_id, fileName: try ImageDisk.cachedName(image)))
            }
            await submit(nodeID: task.node_id, projectID: task.project_id, prompt: prompt, images: images)
        } catch { show(error) }
    }
    func discardPending() {
        do { var next = state; next.pending = nil; try persist(next) }
        catch { show(error) }
    }
    func cancelTask(_ id: String) async {
        do {
            let api = try client()
            let expectedGeneration = generation
            let _: CancelResponse = try await api.post("api/v1/tasks/\(id)/cancel", body: [:])
            guard expectedGeneration == generation else { return }
            try await refreshDetail(id, api: api)
        } catch { show(error) }
    }
    private func validate(_ text: String) throws {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw AppFailure.server(422, "invalid_field", "请输入任务内容。") }
        guard text.utf8.count <= 16 * 1024 else { throw AppFailure.tooLong }
    }
    private func show(_ error: Error) { if !(error is CancellationError) { errorMessage = error.localizedDescription } }
    func projectName(_ id: String, nodeID: String? = nil) -> String {
        let pieces = id.split(separator: "|", maxSplits: 1).map(String.init)
        let node = nodeID ?? (pieces.count == 2 ? pieces[0] : nil)
        let projectID = pieces.count == 2 ? pieces[1] : id
        let project = projects.first { $0.project_id == projectID && (node == nil || $0.node_id == node) }
        let name = project?.name ?? "项目任务"
        if nodes.count > 1, let node, let device = nodes.first(where: { $0.id == node }) { return name + " · " + device.name }
        return name
    }

    func fetchAssistantProfile() async throws -> AssistantProfile {
        let expectedGeneration = generation
        let profile: AssistantProfile = try await client().get("api/v1/assistant-profile")
        guard expectedGeneration == generation else { throw CancellationError() }
        guard profile.revision >= 0, profile.validationMessage == nil else { throw AppFailure.protocolMismatch }
        var next = state; next.assistantProfile = profile
        try persist(next)
        return profile
    }

    func saveAssistantProfile(_ profile: AssistantProfile) async throws -> AssistantProfile {
        if let message = profile.validationMessage { throw AppFailure.network(message) }
        let expectedGeneration = generation
        let saved = try await client().saveProfile(profile)
        guard expectedGeneration == generation else { throw CancellationError() }
        guard saved.revision > profile.revision, saved.validationMessage == nil else { throw AppFailure.protocolMismatch }
        var next = state; next.assistantProfile = saved
        try persist(next)
        return saved
    }

    func loadFile(_ file: OutputFile, taskID: String, forceDownload: Bool = false) async throws -> URL {
        let api = try client()
        let endpoint = api.baseURL.absoluteString
        if !forceDownload, let cached = try fileDisk.cached(file, endpoint: endpoint) { return cached }
        let expectedGeneration = generation
        let temporary = try await api.fileDownload(file, taskID: taskID)
        defer { try? FileManager.default.removeItem(at: temporary) }
        try Task.checkCancellation()
        guard expectedGeneration == generation else { throw CancellationError() }
        return try fileDisk.install(temporary, file: file, endpoint: endpoint)
    }

    func refreshTaskFiles(_ taskID: String) async throws -> TaskFilesResponse {
        guard XNotificationID.safeTask(taskID) else { throw AppFailure.protocolMismatch }
        let expectedGeneration = generation
        let response: TaskFilesResponse = try await client().get("api/v1/tasks/\(taskID)/files")
        guard expectedGeneration == generation else { throw CancellationError() }
        guard response.files.count <= 4, response.files.allSatisfy(\.isValid),
              Set(response.files.map(\.id)).count == response.files.count,
              ["unknown", "none", "pending", "ready", "partial", "failed"].contains(response.transfer_state) else { throw AppFailure.protocolMismatch }
        var next = state
        if var cache = next.caches[taskID], var snapshot = cache.snapshot {
            snapshot.output_files = response.files
            snapshot.file_transfer = FileTransfer(state: response.transfer_state, note: response.note)
            cache.snapshot = snapshot; next.caches[taskID] = cache
            try persist(next)
        }
        return response
    }

    func retryFileDelivery(taskID: String) async throws -> FileRetryResponse {
        guard XNotificationID.safeTask(taskID) else { throw AppFailure.protocolMismatch }
        let expectedGeneration = generation
        let response: FileRetryResponse = try await client().post("api/v1/tasks/\(taskID)/files/retry", body: [:])
        guard expectedGeneration == generation else { throw CancellationError() }
        _ = try await refreshTaskFiles(taskID)
        return response
    }

    func projectEditDraft(nodeID: String, projectID: String) -> ProjectEditDraft? {
        state.projectEditDrafts[ProjectContext.key(nodeID: nodeID, projectID: projectID)]
    }
    func persistProjectEditDraft(_ draft: ProjectEditDraft?, nodeID: String, projectID: String) throws {
        var next = state
        next.projectEditDrafts[ProjectContext.key(nodeID: nodeID, projectID: projectID)] = draft
        try persist(next)
    }
    private func checkedProjectContext(_ value: ProjectContext, nodeID: String, projectID: String) throws {
        guard value.node_id == nodeID, value.project_id == projectID,
              value.validationMessage == nil else { throw AppFailure.protocolMismatch }
    }
    func fetchProjectContext(nodeID: String, projectID: String) async throws -> ProjectContext {
        let expectedGeneration = generation
        do {
            let value: ProjectContext = try await client().get(APIClient.projectPath(nodeID: nodeID, projectID: projectID))
            guard expectedGeneration == generation else { throw CancellationError() }
            try checkedProjectContext(value, nodeID: nodeID, projectID: projectID)
            var next = state; next.projectContexts[ProjectContext.key(nodeID: nodeID, projectID: projectID)] = value
            try persist(next)
            return value
        } catch AppFailure.server(404, _, _) {
            throw AppFailure.network("当前中枢尚未提供此项目的背景设置。聊天仍可继续，编辑稿会保留。")
        }
    }
    func saveProjectContext(_ value: ProjectContext, nodeID: String, projectID: String) async throws -> ProjectContext {
        if let message = value.validationMessage { throw AppFailure.network(message) }
        let expectedGeneration = generation
        let saved = try await client().saveProjectContext(value, nodeID: nodeID, projectID: projectID)
        guard expectedGeneration == generation else { throw CancellationError() }
        try checkedProjectContext(saved, nodeID: nodeID, projectID: projectID)
        guard saved.revision > value.revision else { throw AppFailure.protocolMismatch }
        var next = state
        let key = ProjectContext.key(nodeID: nodeID, projectID: projectID)
        next.projectContexts[key] = saved; next.projectEditDrafts.removeValue(forKey: key)
        try persist(next)
        return saved
    }
    func projectSources(nodeID: String, projectID: String, query: String) async throws -> [ProjectSource] {
        let expectedGeneration = generation
        let path = try APIClient.projectPath(nodeID: nodeID, projectID: projectID) + "/sources"
        let response: ProjectSourcesResponse = try await client().get(path, query: [URLQueryItem(name: "query", value: query), URLQueryItem(name: "limit", value: "20")])
        guard expectedGeneration == generation else { throw CancellationError() }
        guard response.sources.count <= 20, response.sources.allSatisfy({ XNotificationID.safeTask($0.id) && ["done", "failed", "cancelled", "timed_out", "interrupted"].contains($0.status) }),
              Set(response.sources.map(\.id)).count == response.sources.count else { throw AppFailure.protocolMismatch }
        return response.sources
    }
    func createProjectSummary(nodeID: String, projectID: String, requestID: String, sourceIDs: [String]) async throws -> String {
        guard !sourceIDs.isEmpty, sourceIDs.count <= 10, Set(sourceIDs).count == sourceIDs.count,
              sourceIDs.allSatisfy(XNotificationID.safeTask) else { throw AppFailure.protocolMismatch }
        let expectedGeneration = generation
        let result = try await client().createProjectSummary(nodeID: nodeID, projectID: projectID, requestID: requestID, sourceIDs: sourceIDs)
        guard expectedGeneration == generation else { throw CancellationError() }
        guard XNotificationID.safeTask(result.task_id) else { throw AppFailure.protocolMismatch }
        return result.task_id
    }
    func projectSummary(nodeID: String, projectID: String, taskID: String) async throws -> ProjectSummaryResponse {
        guard XNotificationID.safeTask(taskID) else { throw AppFailure.protocolMismatch }
        let expectedGeneration = generation
        let path = try APIClient.projectPath(nodeID: nodeID, projectID: projectID) + "/summary/\(taskID)"
        let value: ProjectSummaryResponse = try await client().get(path)
        guard expectedGeneration == generation else { throw CancellationError() }
        guard value.task_id == taskID, value.source_task_ids.count <= 10,
              value.source_task_ids.allSatisfy(XNotificationID.safeTask), (value.text ?? "").utf8.count <= 4096 else { throw AppFailure.protocolMismatch }
        return value
    }

    private func resetNotificationConnection() {
        notificationWork?.cancel()
        inboxRequestVersion += 1; notificationRouteVersion += 1
        isRefreshingInbox = false; isLoadingMoreInbox = false
        inboxItems = []; unreadCount = 0; inboxNextCursor = nil; inboxNote = nil
        hasLoadedOlderInboxPages = false
        knownInboxResults = [:]; displayedInboxResults = [:]; inboxReads = []
        confirmedDisplayedTaskID = nil; lookedUpDisplayedTaskID = nil; pendingNotification = nil; notificationRoute = nil
        serverID = nil; pushAvailable = nil; notificationRegistered = false
        registeredNotificationToken = nil; notificationNote = nil
    }

    private func acceptServerIdentity(_ id: String) throws {
        guard XNotificationID.matches(id, prefix: "s") else { throw AppFailure.protocolMismatch }
        if let serverID, serverID != id {
            inboxItems = []; unreadCount = 0; inboxNextCursor = nil
            knownInboxResults = [:]; displayedInboxResults = [:]; confirmedDisplayedTaskID = nil
            notificationRoute = nil; notificationRegistered = false; registeredNotificationToken = nil
            var next = state; next.inbox = nil
            try persist(next)
        }
        serverID = id
    }

    private func persistInbox() throws {
        guard let serverID else { return }
        var next = state
        next.inbox = SavedInbox(serverID: serverID, unreadCount: unreadCount, items: inboxItems,
                                nextCursor: inboxNextCursor)
        try persist(next)
    }

    func refreshInbox(automatically: Bool = false) async {
        guard hasConnection, !connectionChanging, !isRefreshingInbox, !isLoadingMoreInbox else { return }
        guard !automatically || !hasLoadedOlderInboxPages else { return }
        isRefreshingInbox = true
        inboxRequestVersion += 1
        let version = inboxRequestVersion
        let expectedGeneration = generation
        let mutationVersion = inboxMutationVersion
        defer { if version == inboxRequestVersion { isRefreshingInbox = false } }
        do {
            let api = try client()
            let response: InboxResponse = try await api.get("api/v1/inbox", query: [URLQueryItem(name: "limit", value: "30")])
            try Task.checkCancellation()
            guard expectedGeneration == generation, version == inboxRequestVersion,
                  mutationVersion == inboxMutationVersion, !connectionChanging else { return }
            try validateInbox(response)
            try acceptServerIdentity(response.server_id)
            inboxItems = response.items
            hasLoadedOlderInboxPages = false
            unreadCount = response.unread_count; inboxNextCursor = response.next_cursor
            for item in response.items { knownInboxResults[item.task_id] = item }
            try persistInbox()
            inboxNote = nil
            // The UI may have finished positioning before the first inbox response.
            // Refresh only that selected round before confirming its now-known result.
            if foreground, let id = confirmedDisplayedTaskID, id == activeTaskID,
               let item = knownInboxResults[id], item.read_at == nil,
               let snapshot = state.caches[id]?.snapshot, snapshot.terminal {
                try await refreshDetail(id, api: api)
                guard expectedGeneration == generation, activeTaskID == id, foreground else { return }
                await confirmInboxResultDisplayed(taskID: id)
            }
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation, version == inboxRequestVersion else { return }
            if case AppFailure.server(404, _, _) = error {
                inboxNote = "这个中枢尚未提供结果收件箱，任务和历史仍可正常使用。"
            } else { inboxNote = "暂时无法同步未查看结果，已保留手机记录。可稍后刷新。" }
        }
    }

    private func validateInbox(_ response: InboxResponse) throws {
        guard XNotificationID.matches(response.server_id, prefix: "s"), response.unread_count >= 0,
              response.items.allSatisfy({ $0.isValid && $0.read_at == nil }),
              Set(response.items.map(\.id)).count == response.items.count,
              Set(response.items.map(\.task_id)).count == response.items.count,
              response.items.count <= response.unread_count else { throw AppFailure.protocolMismatch }
    }

    func loadMoreInbox() async {
        guard hasConnection, !connectionChanging, var cursor = inboxNextCursor,
              !isRefreshingInbox, !isLoadingMoreInbox else { return }
        isLoadingMoreInbox = true
        let version = inboxRequestVersion
        let expectedGeneration = generation
        let mutationVersion = inboxMutationVersion
        defer { if version == inboxRequestVersion { isLoadingMoreInbox = false } }
        do {
            let api = try client()
            var visited: Set<String> = []
            while true {
                guard visited.insert(cursor).inserted else { throw AppFailure.protocolMismatch }
                let response: InboxResponse = try await api.get("api/v1/inbox", query: [
                    URLQueryItem(name: "limit", value: "30"), URLQueryItem(name: "cursor", value: cursor)
                ])
                try Task.checkCancellation()
                guard expectedGeneration == generation, version == inboxRequestVersion,
                      mutationVersion == inboxMutationVersion, !connectionChanging else { return }
                try validateInbox(response)
                guard response.server_id == serverID else { throw AppFailure.protocolMismatch }
                let ids = Set(inboxItems.map(\.id))
                let incoming = response.items.filter { !ids.contains($0.id) }
                let replacements = Dictionary(uniqueKeysWithValues: response.items.map { ($0.id, $0) })
                inboxItems = inboxItems.map { replacements[$0.id] ?? $0 } + incoming
                hasLoadedOlderInboxPages = true
                for item in response.items { knownInboxResults[item.task_id] = item }
                unreadCount = response.unread_count; inboxNextCursor = response.next_cursor
                if response.unread_count == 0 { inboxItems = [] }
                try persistInbox()
                guard incoming.isEmpty, let following = response.next_cursor else { break }
                cursor = following
            }
            inboxNote = nil
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation else { return }
            inboxNote = "暂时无法读取更早的未查看结果，可稍后重试。"
        }
    }

    func consumeNotificationRoute() { notificationRoute = nil }

    func openInboxItem(_ item: InboxItem) async {
        guard item.isValid, hasConnection, !connectionChanging else { return }
        await routeToResult(resultID: item.id, expectedServerID: serverID, expectedTaskID: item.task_id)
    }

    func handleNotification(_ payload: XNotificationPayload) async {
        guard payload.isValid, hasConnection, !connectionChanging else { return }
        if !foreground { pendingNotification = payload; return }
        if let interactionID = payload.interaction_id {
            notificationRouteVersion += 1
            let version = notificationRouteVersion, expectedGeneration = generation
            do {
                let api = try client()
                let settings: NotificationSettings = try await api.get("api/v1/notification-settings")
                guard generation == expectedGeneration, version == notificationRouteVersion, foreground, !connectionChanging else { return }
                guard settings.server_id == payload.server_id else { throw AppFailure.protocolMismatch }
                try await refreshDetail(payload.task_id, api: api)
                guard generation == expectedGeneration, version == notificationRouteVersion, foreground, !connectionChanging else { return }
                guard let item = state.caches[payload.task_id]?.snapshot?.interactions?.first(where: { $0.id == interactionID }),
                      item.isValid, item.revision == payload.revision else { throw AppFailure.protocolMismatch }
                requestedInteractionID = interactionID
                notificationRoute = .init(taskID: payload.task_id, resultID: "", revision: payload.revision,
                                          serverID: settings.server_id, interactionID: interactionID)
            } catch {
                if generation == expectedGeneration, version == notificationRouteVersion {
                    errorMessage = "暂时无法打开待处理提醒，请检查中枢连接或从历史查看；未执行任何批准。"
                }
            }
            return
        }
        await routeToResult(resultID: payload.result_id, expectedServerID: payload.server_id, expectedTaskID: payload.task_id)
    }

    private func routeToResult(resultID: String, expectedServerID: String?, expectedTaskID: String) async {
        guard hasConnection, !connectionChanging, let expectedServerID else { return }
        notificationRouteVersion += 1
        let version = notificationRouteVersion
        let expectedGeneration = generation
        do {
            let api = try client()
            let settings: NotificationSettings = try await api.get("api/v1/notification-settings")
            try Task.checkCancellation()
            guard expectedGeneration == generation, version == notificationRouteVersion, !connectionChanging, foreground else { return }
            guard settings.server_id == expectedServerID else {
                inboxNote = "这条提醒属于其他中枢，当前连接和阅读位置未改变。"
                errorMessage = inboxNote
                return
            }
            try acceptServerIdentity(settings.server_id)
            pushAvailable = settings.push_available
            let response: InboxItemResponse = try await api.get("api/v1/inbox/\(resultID)")
            try Task.checkCancellation()
            guard expectedGeneration == generation, version == notificationRouteVersion, !connectionChanging, foreground else { return }
            guard response.server_id == settings.server_id, response.item.isValid,
                  response.item.id == resultID, response.item.task_id == expectedTaskID else { throw AppFailure.protocolMismatch }
            knownInboxResults[expectedTaskID] = response.item
            try await refreshDetail(expectedTaskID, api: api)
            guard expectedGeneration == generation, version == notificationRouteVersion, !connectionChanging, foreground,
                  let task = state.caches[expectedTaskID]?.snapshot, task.id == expectedTaskID,
                  task.node_id == response.item.node_id, task.project_id == response.item.project_id,
                  task.terminal, task.status == response.item.status else { throw CancellationError() }
            displayedInboxResults[expectedTaskID] = response.item
            notificationRoute = NotificationRoute(taskID: expectedTaskID, resultID: resultID,
                                                  revision: response.item.revision, serverID: response.server_id)
            inboxNote = nil
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation, version == notificationRouteVersion else { return }
            inboxNote = "暂时无法打开这条结果，当前位置已保留。请恢复连接后重试。"
            errorMessage = inboxNote
        }
    }

    func confirmInboxResultDisplayed(taskID: String) async {
        guard foreground, hasConnection, !connectionChanging, activeTaskID == taskID,
              !conversationLoading, let snapshot = state.caches[taskID]?.snapshot, snapshot.terminal else { return }
        confirmedDisplayedTaskID = taskID
        guard let item = displayedInboxResults[taskID] else {
            // Explicitly displayed history may be older than the first inbox page.
            // Look up this round once per selection; polling never scans older pages.
            guard lookedUpDisplayedTaskID != taskID, XNotificationID.safeTask(taskID) else { return }
            lookedUpDisplayedTaskID = taskID
            let expectedGeneration = generation
            do {
                let api = try client()
                let response: InboxItemResponse = try await api.get("api/v1/inbox/by-task/\(taskID)")
                try Task.checkCancellation()
                guard expectedGeneration == generation, activeTaskID == taskID, foreground, !connectionChanging else { return }
                guard response.item.isValid, response.item.task_id == taskID else { throw AppFailure.protocolMismatch }
                try acceptServerIdentity(response.server_id)
                knownInboxResults[taskID] = response.item
                try await refreshDetail(taskID, api: api)
                guard expectedGeneration == generation, activeTaskID == taskID, foreground, !connectionChanging else { return }
                if let displayed = displayedInboxResults[taskID] { await markInboxRead(displayed) }
            } catch is CancellationError { }
            catch {
                guard expectedGeneration == generation, activeTaskID == taskID else { return }
                if case AppFailure.server(404, _, _) = error { return }
                lookedUpDisplayedTaskID = nil // A later explicit display can retry a failed lookup.
                inboxNote = "已保留这条未查看结果，确认暂未完成。可重新打开后重试。"
            }
            return
        }
        await markInboxRead(item)
    }

    func markInboxRead(_ item: InboxItem) async {
        guard foreground, hasConnection, !connectionChanging, activeTaskID == item.task_id,
              !conversationLoading, let snapshot = state.caches[item.task_id]?.snapshot,
              snapshot.terminal, snapshot.status == item.status,
              snapshot.node_id == item.node_id, snapshot.project_id == item.project_id,
              displayedInboxResults[item.task_id] == item, item.read_at == nil,
              knownInboxResults[item.task_id]?.revision == item.revision,
              let serverID else { return }
        let key = "\(item.id):\(item.revision)"
        guard inboxReads.insert(key).inserted else { return }
        defer { inboxReads.remove(key) }
        let expectedGeneration = generation
        do {
            let response = try await client().confirmInboxRead(id: item.id, revision: item.revision)
            try Task.checkCancellation()
            guard expectedGeneration == generation, !connectionChanging else { return }
            guard response.server_id == serverID, response.item.id == item.id,
                  response.item.task_id == item.task_id, response.item.revision == item.revision,
                  response.item.read_at != nil, response.item.isValid else { throw AppFailure.protocolMismatch }
            knownInboxResults[item.task_id] = response.item
            displayedInboxResults[item.task_id] = response.item
            inboxMutationVersion += 1
            inboxItems.removeAll { $0.id == item.id }
            unreadCount = max(0, unreadCount - 1)
            try persistInbox()
            inboxNote = nil
            await refreshInbox(automatically: true)
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation else { return }
            inboxNote = "已保留这条未查看结果，确认暂未完成。可重新打开后重试。"
        }
    }

    func refreshNotificationPermission() async {
        let permission = await notificationSystem.permission()
        notificationPermission = permission
    }

    func refreshNotificationSettings() async {
        guard hasConnection, !connectionChanging else { return }
        let expectedGeneration = generation
        do {
            let settings: NotificationSettings = try await client().get("api/v1/notification-settings")
            try Task.checkCancellation()
            guard expectedGeneration == generation, !connectionChanging else { return }
            try acceptServerIdentity(settings.server_id)
            pushAvailable = settings.push_available
            updateNotificationNote()
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation else { return }
            notificationNote = "暂时无法读取中枢通知设置，任务仍可正常使用。"
        }
    }

    private func refreshNotificationStateSoon() {
        notificationWork?.cancel()
        notificationWork = Task { [weak self] in
            guard let self else { return }
            await refreshNotificationPermission()
            guard !Task.isCancelled, foreground, hasConnection else { return }
            if notificationsEnabled, notificationPermission.allowed { notificationSystem.register() }
            await syncNotificationDevice()
        }
    }

    func requestNotificationPermission() async { await setNotificationsEnabled(true) }

    func setNotificationsEnabled(_ enabled: Bool) async {
        guard !isUpdatingNotifications else { return }
        isUpdatingNotifications = true
        defer { isUpdatingNotifications = false }
        let expectedGeneration = generation
        do {
            var next = state; next.notificationsEnabled = enabled
            try persist(next)
            notificationPermission = await notificationSystem.permission()
            if enabled, notificationPermission == .notDetermined {
                notificationPermission = try await notificationSystem.requestPermission()
            }
            guard expectedGeneration == generation else { return }
            if enabled, notificationPermission.allowed { notificationSystem.register() }
            await syncNotificationDevice()
            updateNotificationNote()
        } catch {
            guard expectedGeneration == generation else { return }
            notificationNote = "通知设置暂未完成，任务仍可正常使用。"
        }
    }

    func retryNotificationRegistration() async {
        guard !isUpdatingNotifications else { return }
        isUpdatingNotifications = true
        defer { isUpdatingNotifications = false }
        await refreshNotificationPermission()
        if notificationsEnabled, notificationPermission.allowed { notificationSystem.register() }
        registeredNotificationToken = nil
        await syncNotificationDevice()
    }

    func didRegisterNotificationToken(_ token: String) async {
        guard XNotificationID.validToken(token) else { notificationRegistrationFailed(); return }
        notificationToken = token
        await syncNotificationDevice()
    }

    func notificationRegistrationFailed() {
        notificationToken = nil; notificationRegistered = false; registeredNotificationToken = nil
        if pushAvailable == false { updateNotificationNote() }
        else { notificationNote = "系统暂未完成推送登记，可稍后重试。任务与未查看结果仍可使用。" }
    }

    private func syncNotificationDevice() async {
        notificationDeviceSyncRequested = true
        if let work = notificationDeviceWork { await work.value; return }
        let work = Task { [weak self] in
            guard let self else { return }
            while notificationDeviceSyncRequested {
                notificationDeviceSyncRequested = false
                await performNotificationDeviceSync()
            }
        }
        notificationDeviceWork = work
        await work.value
        notificationDeviceWork = nil
    }

    // Serialize actual device mutations, independently of the coalescing worker.
    // A connection's DELETE must follow any in-flight PUT even after its generation changes.
    private func orderedNotificationWrite(_ operation: @escaping () async throws -> NotificationDeviceResponse) async throws -> NotificationDeviceResponse {
        let previous = notificationWriteTail
        let write = Task {
            if let previous { await previous.value }
            return try await operation()
        }
        notificationWriteTail = Task { _ = try? await write.value }
        return try await write.value
    }

    private func performNotificationDeviceSync() async {
        guard hasConnection, !connectionChanging else { return }
        if let notificationRemoval { await notificationRemoval.value }
        guard hasConnection, !connectionChanging else { return }
        let expectedGeneration = generation
        let desired = notificationsEnabled && notificationPermission.allowed
        do {
            let api = try client()
            let settings: NotificationSettings = try await api.get("api/v1/notification-settings")
            guard expectedGeneration == generation, !connectionChanging else { return }
            try acceptServerIdentity(settings.server_id)
            pushAvailable = settings.push_available
            if desired {
                guard let notificationToken else { updateNotificationNote(); return }
                guard let environment = Bundle.main.object(forInfoDictionaryKey: "XVIS_APNS_ENVIRONMENT") as? String,
                      ["sandbox", "production"].contains(environment) else {
                    notificationNote = "当前构建未配置推送环境，任务与未查看结果仍可使用。"
                    return
                }
                if notificationRegistered && registeredNotificationToken == notificationToken { updateNotificationNote(); return }
                let deviceID = state.notificationDeviceID
                let result = try await orderedNotificationWrite {
                    try await api.saveNotificationDevice(id: deviceID, token: notificationToken,
                                                         environment: environment, enabled: true)
                }
                guard expectedGeneration == generation, desired == (notificationsEnabled && notificationPermission.allowed),
                      !connectionChanging else { notificationDeviceSyncRequested = true; return }
                guard result.server_id == settings.server_id, result.device_id == state.notificationDeviceID,
                      result.enabled else { throw AppFailure.protocolMismatch }
                notificationRegistered = true; registeredNotificationToken = notificationToken
                pushAvailable = result.push_available
            } else {
                let deviceID = state.notificationDeviceID
                let result = try await orderedNotificationWrite { try await api.deleteNotificationDevice(id: deviceID) }
                guard expectedGeneration == generation, desired == (notificationsEnabled && notificationPermission.allowed),
                      !connectionChanging else { notificationDeviceSyncRequested = true; return }
                guard result.server_id == settings.server_id, result.device_id == state.notificationDeviceID,
                      !result.enabled else { throw AppFailure.protocolMismatch }
                notificationRegistered = false; registeredNotificationToken = nil; pushAvailable = result.push_available
            }
            updateNotificationNote()
        } catch is CancellationError { }
        catch {
            guard expectedGeneration == generation else { return }
            notificationRegistered = false
            notificationNote = "通知登记暂未完成，可稍后重试。任务与未查看结果仍可使用。"
        }
    }

    private func updateNotificationNote() {
        if !hasConnection { notificationNote = "连接中枢后可设置推送；任务不会因通知权限受到限制。" }
        else if pushAvailable == false {
            notificationNote = notificationRegistered
                ? "设备登记已保存；中枢尚未配置推送，暂不会收到远程提醒。"
                : "中枢尚未配置推送，暂不会收到远程提醒。任务与未查看结果仍可使用。"
        }
        else if !notificationsEnabled { notificationNote = "提醒已关闭，可从未查看结果领取成果。" }
        else if !notificationPermission.allowed { notificationNote = "系统通知未允许，可在系统设置中开启。任务仍可使用。" }
        else if notificationToken == nil { notificationNote = "正在等待系统推送登记，任务仍可正常使用。" }
        else if !notificationRegistered { notificationNote = "推送设备尚未完成中枢登记，可重试。" }
        else { notificationNote = "通知登记已完成。提醒可能延迟或漏收，未查看结果仍会保留。" }
    }
}
