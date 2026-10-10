import XCTest
@testable import XVIS

final class NotificationTests: XCTestCase {
    func testPayloadRejectsMalformedIdentityPathsAndNonIntegerVersions() {
        let valid: [String: Any] = ["server_id": NFixture.serverA, "task_id": "round-1", "result_id": NFixture.item(1).id, "revision": 1]
        XCTAssertEqual(XNotificationPayload.parse(["xvis": valid])?.task_id, "round-1")
        for (key, value) in [("server_id", "s-wrong" as Any), ("result_id", "../task" as Any),
                             ("task_id", "https://other.example.com/" as Any), ("revision", true as Any), ("revision", 1.5 as Any)] {
            var invalid = valid; invalid[key] = value
            XCTAssertNil(XNotificationPayload.parse(["xvis": invalid]))
        }
        XCTAssertNil(XNotificationPayload.parse([:]))
        XCTAssertTrue(XNotificationID.validToken(String(repeating: "a", count: 32)))
        XCTAssertTrue(XNotificationID.validToken(String(repeating: "b", count: 128)))
        XCTAssertFalse(XNotificationID.validToken("PRIVATE-TOKEN"))
    }

    func testLegacyStateDefaultsAndNotificationMetadataNeverPersistToken() throws {
        let old = try JSONDecoder().decode(SavedState.self, from: Data("{\"baseURL\":\"https://example.com/xvis/\"}".utf8))
        XCTAssertFalse(old.notificationsEnabled)
        XCTAssertNil(old.inbox)
        XCTAssertTrue(XNotificationID.matches(old.notificationDeviceID, prefix: "d"))
        var current = old
        current.notificationsEnabled = true
        current.inbox = SavedInbox(serverID: NFixture.serverA, unreadCount: 1, items: [NFixture.item(1)], nextCursor: nil)
        let restored = try JSONDecoder().decode(SavedState.self, from: JSONEncoder().encode(current))
        XCTAssertEqual(restored.notificationDeviceID, old.notificationDeviceID)
        XCTAssertEqual(restored.inbox?.items, [NFixture.item(1)])
        XCTAssertTrue(restored.notificationsEnabled)
    }

    @MainActor func testFirstPageIsAuthoritativeAfterAnotherClientReadsAnItem() async throws {
        let fixture = try NFixture(count: 2)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        await store.refreshInbox()
        XCTAssertEqual(store.inboxItems.count, 2)
        fixture.markExternallyRead(1)
        await store.refreshInbox()
        XCTAssertEqual(store.inboxItems.map(\.task_id), ["round-2"])
        XCTAssertEqual(store.unreadCount, 1)
        XCTAssertEqual(try fixture.disk.load().inbox?.items.map(\.task_id), ["round-2"])
    }

    @MainActor func testInboxPaginationAndRefreshResetToAuthoritativeHead() async throws {
        let fixture = try NFixture(count: 35)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        await store.refreshInbox()
        XCTAssertEqual(store.inboxItems.count, 30)
        XCTAssertEqual(store.unreadCount, 35)
        await store.loadMoreInbox()
        XCTAssertEqual(store.inboxItems.count, 35)
        XCTAssertNil(store.inboxNextCursor)
        fixture.markExternallyRead(1)
        await store.refreshVisible()
        XCTAssertEqual(store.inboxItems.count, 35) // Automatic polling preserves the expanded reading window.
        XCTAssertEqual(store.unreadCount, 35)
        await store.refreshInbox()
        XCTAssertEqual(store.inboxItems.count, 30)
        XCTAssertEqual(store.unreadCount, 34) // Explicit refresh restores the server's authoritative page.
        XCTAssertNotNil(store.inboxNextCursor)
    }

    @MainActor func testOnlySelectedSuccessfullyLoadedRoundIsConfirmedAndBackgroundCannotRead() async throws {
        let fixture = try NFixture(count: 3, linked: true)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.refreshInbox()
        await store.loadConversation(containing: "round-2")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-2"), ["round-1", "round-2", "round-3"])
        await store.confirmInboxResultDisplayed(taskID: "round-1")
        XCTAssertTrue(fixture.reads.isEmpty)
        await store.confirmInboxResultDisplayed(taskID: "round-2")
        XCTAssertEqual(fixture.reads, ["round-2"])
        XCTAssertEqual(store.unreadCount, 2)
        await store.loadConversation(containing: "round-3")
        store.setForeground(false)
        await store.confirmInboxResultDisplayed(taskID: "round-3")
        XCTAssertEqual(fixture.reads, ["round-2"])
    }

    @MainActor func testOrdinaryHistoryOutsideFirstPageUsesOneByTaskLookupWithoutPageScanning() async throws {
        let fixture = try NFixture(count: 35)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.refreshInbox()
        XCTAssertFalse(store.inboxItems.contains { $0.task_id == "round-35" })
        await store.loadConversation(containing: "round-35")
        await store.confirmInboxResultDisplayed(taskID: "round-35")
        await store.confirmInboxResultDisplayed(taskID: "round-35")
        XCTAssertEqual(fixture.byTaskRequests, ["round-35"])
        XCTAssertEqual(fixture.reads, ["round-35"])
        XCTAssertEqual(fixture.cursorRequests, 0)
        XCTAssertEqual(store.unreadCount, 34)
        store.setForeground(false)
    }

    @MainActor func testLegacyTaskWithNoInboxRecordIsNormalAndLookedUpOnlyOnce() async throws {
        let fixture = try NFixture(count: 1)
        defer { fixture.cleanUp() }
        fixture.omitResult = true
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.loadConversation(containing: "round-1")
        await store.confirmInboxResultDisplayed(taskID: "round-1")
        await store.confirmInboxResultDisplayed(taskID: "round-1")
        XCTAssertEqual(fixture.byTaskRequests, ["round-1"])
        XCTAssertTrue(fixture.reads.isEmpty)
        XCTAssertNil(store.errorMessage)
        store.setForeground(false)
    }

    @MainActor func testNotificationFetchesAuthoritativeVersionAndDoesNotConfirmBeforeDisplay() async throws {
        let fixture = try NFixture(count: 1)
        defer { fixture.cleanUp() }
        fixture.setRevision(3, for: 1)
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.handleNotification(fixture.payload(1, revision: 1))
        XCTAssertEqual(store.notificationRoute?.taskID, "round-1")
        XCTAssertEqual(store.notificationRoute?.revision, 3)
        XCTAssertNil(store.activeTaskID)
        XCTAssertTrue(fixture.reads.isEmpty)
        store.consumeNotificationRoute()
        await store.loadConversation(containing: "round-1")
        await store.confirmInboxResultDisplayed(taskID: "round-1")
        XCTAssertEqual(fixture.reads, ["round-1"])
        XCTAssertEqual(fixture.readVersions, [3])
        store.setForeground(false)
    }

    @MainActor func testOtherServerAndUnconnectedNotificationCannotLoadOrNavigate() async throws {
        let fixture = try NFixture(count: 1)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        var wrong = fixture.payload(1); wrong.server_id = NFixture.serverB
        await store.handleNotification(wrong)
        XCTAssertNil(store.notificationRoute)
        XCTAssertTrue(store.state.caches.isEmpty)
        XCTAssertTrue(store.errorMessage?.contains("其他中枢") == true)
        store.clearConnection()
        await store.handleNotification(fixture.payload(1))
        XCTAssertNil(store.notificationRoute)
        XCTAssertTrue(store.state.caches.isEmpty)
        XCTAssertTrue(fixture.reads.isEmpty)
        store.setForeground(false)
    }

    @MainActor func testOldNotificationResponseCannotPolluteChangedConnection() async throws {
        let fixture = try NFixture(count: 1)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        let gate = NReplyGate()
        fixture.taskGate = gate
        let routing = Task { await store.handleNotification(fixture.payload(1)) }
        await fulfillment(of: [gate.received], timeout: 5)
        await store.connect(baseURL: "https://other.example.com/xvis/", newToken: "synthetic-only")
        gate.complete(.json(200, fixture.task(1)))
        await routing.value
        XCTAssertNil(store.notificationRoute)
        XCTAssertTrue(store.state.caches.isEmpty)
        XCTAssertEqual(store.state.baseURL, "https://other.example.com/xvis/")
        store.setForeground(false)
    }

    @MainActor func testReadVersionConflictKeepsUnreadAndDoesNotDecrement() async throws {
        let fixture = try NFixture(count: 1)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.refreshInbox()
        await store.loadConversation(containing: "round-1")
        fixture.conflictRead = true
        await store.confirmInboxResultDisplayed(taskID: "round-1")
        XCTAssertEqual(store.unreadCount, 1)
        XCTAssertEqual(store.inboxItems.count, 1)
        XCTAssertTrue(store.inboxNote?.contains("暂未完成") == true)
        store.setForeground(false)
    }

    @MainActor func testOldInboxRefreshCannotReintroduceAnAcknowledgedResult() async throws {
        let fixture = try NFixture(count: 1)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.refreshInbox()
        await store.loadConversation(containing: "round-1")
        let gate = NReplyGate()
        fixture.inboxGate = gate
        let refresh = Task { await store.refreshInbox() }
        await fulfillment(of: [gate.received], timeout: 5)
        let oldReply = fixture.inboxReply()
        await store.confirmInboxResultDisplayed(taskID: "round-1")
        gate.complete(.json(200, oldReply))
        await refresh.value
        XCTAssertEqual(store.unreadCount, 0)
        XCTAssertTrue(store.inboxItems.isEmpty)
        store.setForeground(false)
    }

    @MainActor func testDeniedPermissionDoesNotBlockSubmissionAndUnconfiguredPushHasPriority() async throws {
        let fixture = try NFixture(count: 0)
        defer { fixture.cleanUp() }
        let system = NSystem(permission: .denied)
        let store = fixture.store(system: system)
        await store.setNotificationsEnabled(true)
        XCTAssertEqual(system.requests, 0)
        XCTAssertEqual(system.registrations, 0)
        XCTAssertTrue(store.notificationNote?.contains("尚未配置推送") == true)
        let submitted = await store.submit(nodeID: "mac-a", projectID: "accept", prompt: "任务不受权限影响")
        XCTAssertNotNil(submitted)
        XCTAssertNil(store.state.pending)
        XCTAssertEqual(store.submittedTaskID, "accepted")
    }

    @MainActor func testRuntimeTokenRegistrationUsesExplicitEnvironmentAndDoesNotPersistToken() async throws {
        let fixture = try NFixture(count: 0)
        defer { fixture.cleanUp() }
        let system = NSystem(permission: .notDetermined)
        let store = fixture.store(system: system)
        await store.setNotificationsEnabled(true)
        XCTAssertEqual(system.requests, 1)
        XCTAssertEqual(system.registrations, 1)
        let token = String(repeating: "a1", count: 32)
        await store.didRegisterNotificationToken(token)
        XCTAssertTrue(store.notificationRegistered)
        XCTAssertEqual(fixture.deviceBodies.last?["token"] as? String, token)
        XCTAssertEqual(fixture.deviceBodies.last?["environment"] as? String, "sandbox")
        XCTAssertFalse(String(decoding: try Data(contentsOf: fixture.disk.file), as: UTF8.self).contains(token))
        await store.setNotificationsEnabled(false)
        XCTAssertFalse(fixture.deviceEnabled)
        XCTAssertFalse(store.notificationRegistered)
    }

    @MainActor func testClearConnectionDeleteWaitsForOldInFlightDeviceWrite() async throws {
        let fixture = try NFixture(count: 0)
        defer { fixture.cleanUp() }
        let store = fixture.store(system: NSystem(permission: .authorized))
        await store.setNotificationsEnabled(true)
        let gate = NReplyGate()
        fixture.deviceGate = gate
        let registering = Task { await store.didRegisterNotificationToken(String(repeating: "c2", count: 32)) }
        await fulfillment(of: [gate.received], timeout: 5)
        fixture.clearDeviceOperations()
        store.clearConnection()
        await Task.yield()
        XCTAssertFalse(fixture.deviceOperations.contains("DELETE"))
        fixture.completeDevicePut(gate)
        await registering.value
        await waitFor { fixture.deviceOperations.contains("DELETE") }
        XCTAssertEqual(fixture.deviceOperations, ["PUT", "DELETE"])
        XCTAssertFalse(fixture.deviceEnabled)
        XCTAssertFalse(store.hasConnection)
    }

    @MainActor func testConnectionSwitchAlsoDeletesOldRegistrationAfterDelayedPut() async throws {
        let fixture = try NFixture(count: 0)
        defer { fixture.cleanUp() }
        let store = fixture.store(system: NSystem(permission: .authorized))
        await store.setNotificationsEnabled(true)
        let gate = NReplyGate()
        fixture.deviceGate = gate
        let registering = Task { await store.didRegisterNotificationToken(String(repeating: "d3", count: 32)) }
        await fulfillment(of: [gate.received], timeout: 5)
        fixture.clearDeviceOperations()
        await store.connect(baseURL: "https://other.example.com/xvis/", newToken: "synthetic-only")
        await Task.yield()
        XCTAssertFalse(fixture.deviceOperations.contains("DELETE"))
        fixture.completeDevicePut(gate)
        await registering.value
        await waitFor { fixture.deviceOperations.contains("DELETE") }
        XCTAssertEqual(fixture.deviceOperations.first, "PUT")
        XCTAssertFalse(fixture.deviceEnabled)
        XCTAssertTrue(store.hasConnection)
        XCTAssertFalse(store.notificationsEnabled)
        store.setForeground(false)
    }

    @MainActor func testForegroundRestoreConfirmsOnlyPreviouslyDisplayedRound() async throws {
        let fixture = try NFixture(count: 3, linked: true)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setForeground(true)
        await settled(store)
        await store.refreshInbox()
        await store.loadConversation(containing: "round-2")
        fixture.conflictRead = true
        await store.confirmInboxResultDisplayed(taskID: "round-2")
        XCTAssertTrue(fixture.reads.isEmpty)
        store.setForeground(false)
        fixture.conflictRead = false
        store.setForeground(true)
        await settled(store)
        await store.refreshInbox()
        XCTAssertEqual(fixture.reads, ["round-2"])
        XCTAssertEqual(store.unreadCount, 2)
        store.setForeground(false)
    }

    @MainActor private func settled(_ store: AppStore) async {
        await waitFor { !store.isRefreshing && !store.isRefreshingInbox }
        await Task.yield()
        await waitFor { !store.isRefreshing && !store.isRefreshingInbox }
    }
    @MainActor private func waitFor(_ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(3)
        while !condition(), Date() < deadline { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(condition())
    }
}

@MainActor private final class NSystem: NotificationSystem {
    var value: NotificationPermission
    var requests = 0
    var registrations = 0
    init(permission: NotificationPermission) { value = permission }
    func permission() async -> NotificationPermission { value }
    func requestPermission() async throws -> NotificationPermission { requests += 1; value = .authorized; return value }
    func register() { registrations += 1 }
}

private final class NCredentials: CredentialStore {
    var value: ConnectionCredential? = ConnectionCredential(endpoint: "https://example.com/xvis/", token: "synthetic-only")
    func read() throws -> ConnectionCredential? { value }
    func save(_ credential: ConnectionCredential) throws { value = credential }
    func clear() throws { value = nil }
}

private final class NFixture {
    static let serverA = "s-" + String(repeating: "a", count: 32)
    static let serverB = "s-" + String(repeating: "b", count: 32)
    let directory: URL
    let disk: StateDisk
    let session: URLSession
    private let route: String
    private let lock = NSRecursiveLock()
    private var records: [InboxItem]
    private let linked: Bool
    private var capturedReads: [String] = []
    private var capturedVersions: [Int] = []
    private var capturedByTask: [String] = []
    private var capturedCursors = 0
    private var capturedDevices: [[String: Any]] = []
    private var savedDevices: [String: Bool] = [:]
    private var deviceOps: [String] = []
    var omitResult = false
    var conflictRead = false
    var taskGate: NReplyGate?
    var inboxGate: NReplyGate?
    var deviceGate: NReplyGate?
    var reads: [String] { lock.withLock { capturedReads } }
    var readVersions: [Int] { lock.withLock { capturedVersions } }
    var byTaskRequests: [String] { lock.withLock { capturedByTask } }
    var cursorRequests: Int { lock.withLock { capturedCursors } }
    var deviceBodies: [[String: Any]] { lock.withLock { capturedDevices } }
    var deviceOperations: [String] { lock.withLock { deviceOps } }
    var deviceEnabled: Bool { lock.withLock { savedDevices["example.com"] ?? false } }

    init(count: Int, linked: Bool = false) throws {
        records = (0..<count).map { Self.item($0 + 1) }; self.linked = linked
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("XVIS-NotificationTests-\(UUID().uuidString)")
        disk = StateDisk(directory: directory)
        var state = SavedState(); state.baseURL = "https://example.com/xvis/"
        try disk.save(state)
        route = UUID().uuidString
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [NProtocol.self]; config.httpAdditionalHeaders = [NProtocol.routeHeader: route]
        session = URLSession(configuration: config)
        NProtocol.register(route) { [weak self] request in
            guard let self else { return .failure(URLError(.cancelled)) }
            return try self.reply(request)
        }
    }
    @MainActor func store(system: NSystem? = nil) -> AppStore {
        AppStore(disk: disk, credentials: NCredentials(), clientFactory: { [session] in APIClient(baseURL: $0, token: $1, session: session) }, notificationSystem: system ?? NSystem(permission: .denied))
    }
    func cleanUp() { NProtocol.unregister(route); try? FileManager.default.removeItem(at: directory) }
    static func item(_ index: Int) -> InboxItem {
        InboxItem(result_id: "r-" + String(format: "%032x", index), task_id: "round-\(index)", node_id: "mac-a", project_id: "accept", title: "测试结果", status: "done", revision: 1, created_at: "2026-10-09T00:00:00Z", read_at: nil)
    }
    func payload(_ index: Int, revision: Int = 1) -> XNotificationPayload {
        XNotificationPayload(server_id: Self.serverA, task_id: "round-\(index)", result_id: Self.item(index).id, revision: revision)
    }
    func markExternallyRead(_ index: Int) { lock.withLock { records[index - 1].read_at = "2026-10-09T01:00:00Z" } }
    func setRevision(_ revision: Int, for index: Int) { lock.withLock { records[index - 1].revision = revision } }
    func task(_ index: Int) -> [String: Any] {
        lock.withLock {
            var value: [String: Any] = ["task_id": "round-\(index)", "node_id": "mac-a", "project_id": "accept", "status": "done", "created_at": "2026-10-09T00:00:00Z", "can_followup": true,
                                        "prompt": "原指令", "result": ["final_text": "真实完整结果", "truncated": false]]
            if linked && index > 1 { value["is_followup_of"] = "round-\(index - 1)" }
            if linked && index < records.count { value["followup_task_id"] = "round-\(index + 1)"; value["can_followup"] = false }
            return value
        }
    }
    func inboxReply(cursor: Bool = false, server: String = serverA) -> [String: Any] {
        lock.withLock {
            let unread = omitResult ? [] : records.filter { $0.read_at == nil }
            let page = cursor ? Array(unread.dropFirst(30)) : Array(unread.prefix(30))
            return ["server_id": server, "unread_count": unread.count, "items": page.map(Self.jsonItem), "next_cursor": (!cursor && unread.count > 30) ? "older" : NSNull()]
        }
    }
    private static func jsonItem(_ item: InboxItem) -> [String: Any] { (try? JSONSerialization.jsonObject(with: JSONEncoder().encode(item))) as? [String: Any] ?? [:] }
    func clearDeviceOperations() { lock.withLock { deviceOps = [] } }
    func completeDevicePut(_ gate: NReplyGate) {
        let reply: [String: Any] = lock.withLock { savedDevices["example.com"] = true; deviceOps.append("PUT"); return deviceReply(Self.serverA, enabled: true) }
        gate.complete(.json(200, reply))
    }
    private func deviceReply(_ server: String, enabled: Bool) -> [String: Any] {
        ["server_id": server, "device_id": (try? disk.load().notificationDeviceID) ?? "", "enabled": enabled, "push_available": false]
    }
    private func reply(_ request: URLRequest) throws -> NReply {
        try lock.withLock {
            let url = try XCTUnwrap(request.url)
            let path = url.path.replacingOccurrences(of: "/xvis/api/v1/", with: "")
            let server = url.host == "other.example.com" ? Self.serverB : Self.serverA
            let method = request.httpMethod ?? "GET"
            if path == "nodes" { return .json(200, ["nodes": []]) }
            if path == "projects" { return .json(200, ["projects": []]) }
            if path == "tasks", method == "GET" { return .json(200, ["tasks": [], "next_cursor": NSNull()]) }
            if path == "tasks", method == "POST" { return .json(200, ["task_id": "accepted", "status": "queued", "node_online": true]) }
            if path == "notification-settings" { return .json(200, ["server_id": server, "push_available": false]) }
            if path.hasPrefix("notification-devices/") {
                if method == "PUT" {
                    let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any] ?? [:]
                    capturedDevices.append(body)
                    if let gate = deviceGate { deviceGate = nil; return .deferred(gate) }
                    savedDevices[url.host ?? ""] = true; deviceOps.append("PUT")
                } else { savedDevices[url.host ?? ""] = false; deviceOps.append("DELETE") }
                return .json(200, deviceReply(server, enabled: savedDevices[url.host ?? ""] ?? false))
            }
            if path == "inbox" {
                let cursor = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains { $0.name == "cursor" } == true
                if cursor { capturedCursors += 1 }
                if let gate = inboxGate { inboxGate = nil; return .deferred(gate) }
                return .json(200, inboxReply(cursor: cursor, server: server))
            }
            if path.hasPrefix("inbox/by-task/") {
                let task = url.lastPathComponent; capturedByTask.append(task)
                guard !omitResult, let item = records.first(where: { $0.task_id == task }) else { return .json(404, ["error": ["code": "not_found", "message": ""]]) }
                return .json(200, ["server_id": server, "item": Self.jsonItem(item)])
            }
            if path.hasPrefix("inbox/") {
                let parts = path.split(separator: "/")
                guard let index = records.firstIndex(where: { $0.id == String(parts[1]) }) else { throw URLError(.badURL) }
                if method == "PUT" {
                    let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any] ?? [:]
                    let revision = body["revision"] as? Int ?? 0
                    if conflictRead || records[index].revision != revision { return .json(409, ["error": ["code": "result_revision_conflict", "message": ""]]) }
                    records[index].read_at = "2026-10-09T01:00:00Z"
                    capturedReads.append(records[index].task_id); capturedVersions.append(revision)
                }
                return .json(200, ["server_id": server, "item": Self.jsonItem(records[index])])
            }
            if path.hasSuffix("/events") {
                let after = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "after" }?.value ?? "0"
                return .json(200, ["events": [], "next_after": Int(after) ?? 0, "has_more": false])
            }
            if path.hasPrefix("tasks/round-") {
                if let gate = taskGate { taskGate = nil; return .deferred(gate) }
                let index = Int(url.lastPathComponent.replacingOccurrences(of: "round-", with: "")) ?? 1
                return .json(200, task(index))
            }
            throw URLError(.unsupportedURL)
        }
    }
}

private enum NReply { case json(Int, [String: Any]), failure(Error), deferred(NReplyGate) }
private final class NReplyGate {
    let received = XCTestExpectation(description: "HTTP request reached the explicit race gate")
    private let lock = NSLock()
    private var connection: NProtocol?
    func attach(_ value: NProtocol) { lock.withLock { connection = value }; received.fulfill() }
    func complete(_ reply: NReply) { let value = lock.withLock { let old = connection; connection = nil; return old }; value?.deliver(reply) }
}
private final class NProtocol: URLProtocol {
    static let routeHeader = "X-XVIS-Notification-Test"
    private static let lock = NSLock()
    private static var routes: [String: (URLRequest) throws -> NReply] = [:]
    private let loadingLock = NSLock()
    private var stopped = false
    static func register(_ key: String, handler: @escaping (URLRequest) throws -> NReply) { lock.withLock { routes[key] = handler } }
    static func unregister(_ key: String) { lock.withLock { _ = routes.removeValue(forKey: key) } }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var normalized = request
        if normalized.httpBody == nil, let stream = normalized.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; bytes.append(contentsOf: buffer.prefix(count)) }
            normalized.httpBody = bytes
        }
        let handler = Self.lock.withLock { Self.routes[normalized.value(forHTTPHeaderField: Self.routeHeader) ?? ""] }
        do { guard let handler else { throw URLError(.unsupportedURL) }; deliver(try handler(normalized)) }
        catch { deliver(.failure(error)) }
    }
    func deliver(_ reply: NReply) {
        guard !loadingLock.withLock({ stopped }) else { return }
        switch reply {
        case .json(let status, let object):
            guard let url = request.url, let bytes = try? JSONSerialization.data(withJSONObject: object),
                  let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"]) else { return }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed); client?.urlProtocol(self, didLoad: bytes); client?.urlProtocolDidFinishLoading(self)
        case .failure(let error): client?.urlProtocol(self, didFailWithError: error)
        case .deferred(let gate): gate.attach(self)
        }
    }
    override func stopLoading() { loadingLock.withLock { stopped = true } }
}
