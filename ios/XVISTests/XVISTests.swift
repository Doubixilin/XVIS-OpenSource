import XCTest
@testable import XVIS

final class XVISTests: XCTestCase {
    func testFreshAndLegacyMissingEndpointRequireExplicitConfiguration() throws {
        XCTAssertTrue(SavedState().baseURL.isEmpty)
        XCTAssertTrue(try JSONDecoder().decode(SavedState.self, from: Data("{}".utf8)).baseURL.isEmpty)
    }

    @MainActor func testFreshInstallDoesNotCreateAClientOrSendRequests() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let credentials = MemoryCredentials(); credentials.value = nil
        var attempts = 0
        let store = AppStore(disk: StateDisk(directory: dir), credentials: credentials,
                             clientFactory: { attempts += 1; return APIClient(baseURL: $0, token: $1) })
        store.setForeground(true)
        let submitted = await store.submit(nodeID: "mac-a", projectID: "p-xvis-test", prompt: "test")
        store.setForeground(false)
        XCTAssertNil(submitted)
        XCTAssertFalse(store.hasConnection)
        XCTAssertTrue(store.state.baseURL.isEmpty)
        XCTAssertEqual(attempts, 0)
    }

    func testMarkdownHeadingsListsAndQuotesKeepTheirContent() {
        let source = "# 中文小结\n\n- 第一项 **重要**\n2. 第二项🙂\n\n> 请保留这段提醒\n"
        let blocks = MarkdownAnswerParser.parse(source)
        XCTAssertTrue(blocks.contains(.heading(level: 1, text: "中文小结")))
        XCTAssertTrue(blocks.contains(.quote("请保留这段提醒")))
        let items = blocks.compactMap { block -> String? in
            if case .listItem(_, let text, _) = block { return text }
            return nil
        }
        XCTAssertEqual(items, ["第一项 **重要**", "第二项🙂"])
    }

    func testMarkdownCodePreservesChineseIndentationAndNewlines() {
        let code = "let 名称 = \"中文🙂\"\n\n  print(名称, \"a|b\")\n"
        let blocks = MarkdownAnswerParser.parse("## 示例\n```swift\n" + code + "```\n")
        XCTAssertTrue(blocks.contains(.heading(level: 2, text: "示例")))
        XCTAssertTrue(blocks.contains(.code(language: "swift", text: code)))
    }

    func testMarkdownTableRetainsEveryCellAndColumnAlignment() {
        let source = "| 项目 | 说明 | 数值 |\n| :--- | :---: | ---: |\n| 第一 | 中文🙂 | 10 |\n| 第二 | **重要** | 20 |\n"
        let blocks = MarkdownAnswerParser.parse(source)
        XCTAssertTrue(blocks.contains(.table(headers: ["项目", "说明", "数值"],
                                             rows: [["第一", "中文🙂", "10"], ["第二", "**重要**", "20"]],
                                             alignments: [.left, .center, .right])))
    }

    func testMarkdownMalformedAndUnsupportedContentRemainsReadable() {
        let unfinishedCode = "```python\nprint(\"中文🙂\")\n"
        XCTAssertTrue(MarkdownAnswerParser.parse(unfinishedCode).contains(.text(unfinishedCode)))
        for source in ["| 标题 | 内容 |\n| -- | invalid |\n| 中文 | 必须保留 |\n",
                       "<details><summary>提醒</summary>\n保留中文🙂\n</details>"] {
            let blocks = MarkdownAnswerParser.parse(source)
            XCTAssertFalse(blocks.contains { if case .table = $0 { return true }; return false })
            let readable = blocks.compactMap { block -> String? in
                if case .text(let text) = block { return text }
                return nil
            }.joined(separator: "\n")
            for line in source.components(separatedBy: "\n") where !line.isEmpty {
                XCTAssertTrue(readable.contains(line), "Missing original content: \(line)")
            }
        }
    }

    func testMarkdownCacheReusesParseForSameSource() {
        let cache = MarkdownAnswerCache()
        let source = "# 缓存\n\n- 第一项🙂\n\n```swift\nlet a = \"中文\"\n```\n"
        let expected = MarkdownAnswerParser.parse(source)
        XCTAssertEqual(cache.blocks(for: source), expected)
        XCTAssertEqual(cache.blocks(for: source), expected)
        XCTAssertEqual(cache.hits, 1)
        XCTAssertEqual(cache.blocks(for: source + "\n追加"), MarkdownAnswerParser.parse(source + "\n追加"))
        XCTAssertEqual(cache.hits, 1)
    }

    func testMarkdownCacheEvictsOldestBeyondCapacity() {
        let cache = MarkdownAnswerCache(capacity: 2)
        _ = cache.blocks(for: "一"); _ = cache.blocks(for: "二"); _ = cache.blocks(for: "三")
        _ = cache.blocks(for: "一") // 「一」已被淘汰，重新解析
        XCTAssertEqual(cache.hits, 0)
        _ = cache.blocks(for: "三")
        XCTAssertEqual(cache.hits, 1)
    }

    func testDefaultWorkspaceSelectsOnlyAvailableTestProject() throws {
        let contract = ProjectView(project_id: "p-example-project", name: "Example Project", node_id: "mac-a", available: true)
        let workspace = ProjectView(project_id: ProjectView.defaultWorkspaceID, name: "测试工作区", node_id: "mac-a", available: true)
        let selected = try XCTUnwrap(ProjectView.defaultWorkspace(in: [contract, workspace]))
        XCTAssertEqual(selected.id, "p-xvis-test")
        XCTAssertEqual(selected.node_id, "mac-a")
    }

    func testDefaultWorkspaceNeverFallsBackToAnotherProject() {
        let contract = ProjectView(project_id: "p-example-project", name: "Example Project", node_id: "mac-a", available: true)
        let unavailable = ProjectView(project_id: ProjectView.defaultWorkspaceID, name: "测试工作区", node_id: "mac-a", available: false)
        XCTAssertNil(ProjectView.defaultWorkspace(in: []))
        XCTAssertNil(ProjectView.defaultWorkspace(in: [contract]))
        XCTAssertNil(ProjectView.defaultWorkspace(in: [contract, unavailable]))
    }

    func testHTTPSBaseURLKeepsProxyPrefix() throws {
        let base = try APIClient.validatedURL("https://example.com/xvis")
        let api = APIClient(baseURL: base, token: "test-only")
        XCTAssertEqual(try api.url(path: "api/v1/tasks").absoluteString, "https://example.com/xvis/api/v1/tasks")
        for invalid in ["http://localhost/", "https://user:secret@example.com/", "https://example.com/?token=x", "https://example.com/#x"] {
            XCTAssertThrowsError(try APIClient.validatedURL(invalid))
        }
    }

    func testPersistedSubmissionRetriesOriginalRequest() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        var state = SavedState(); state.baseURL = "https://example.com/xvis/"
        state.pending = .newTask(nodeID: "mac-a", projectID: "accept", prompt: "生成中文文件\n保留原内容")
        try disk.save(state)
        let restored = try disk.load()
        XCTAssertEqual(restored.pending, state.pending)
        XCTAssertEqual(restored.pending?.body, state.pending?.body)
        XCTAssertEqual(restored.pending?.body["prompt"], "生成中文文件\n保留原内容")
        let persisted = try String(contentsOf: disk.file, encoding: .utf8)
        XCTAssertFalse(persisted.contains("Authorization"))
        XCTAssertFalse(persisted.contains("app-token"))
    }

    func testFollowupHasIndependentRequestAndNoProjectOverride() {
        let request = PendingSubmission.followup(taskID: "original", message: "继续")
        XCTAssertEqual(request.followsTaskID, "original")
        XCTAssertEqual(Set(request.body.keys), ["client_request_id", "message"])
    }

    func testFinalChunksAndCursorSurviveReopen() throws {
        let first = page([event(3, "final_chunk", ["text": .string("第一")]), event(7, "final_chunk", ["text": .string("轮🙂"), "seq_end": .bool(true)])], after: 7)
        let cache = try TaskCache().consuming(first, taskID: "task")
        XCTAssertEqual(cache.finalText, "第一轮🙂")
        XCTAssertTrue(cache.finalComplete)
        XCTAssertEqual(cache.after, 7)
        let restored = try JSONDecoder().decode(TaskCache.self, from: JSONEncoder().encode(cache))
        let next = try restored.consuming(page([], after: 7), taskID: "task")
        XCTAssertEqual(next.finalText, cache.finalText)
        XCTAssertEqual(next.after, 7)
    }

    func testInvalidPageCannotAdvanceCursor() throws {
        let cache = try TaskCache().consuming(page([event(5, "text", ["text": .string("ok")])], after: 5), taskID: "task")
        let wrongTask = EventView(id: 8, task_id: "other", type: "text", payload: [:], created_at: "")
        for invalid in [page([wrongTask], after: 8), page([event(5, "text", [:])], after: 5), page([event(9, "final_chunk", [:])], after: 9), page([], after: 8), page([], after: 5, more: true)] {
            XCTAssertThrowsError(try cache.consuming(invalid, taskID: "task"))
            XCTAssertEqual(cache.after, 5)
            XCTAssertEqual(cache.steps.count, 1)
        }
    }

    func testFinalCapUsesUTF8AndRetainsCompletion() throws {
        let long = String(repeating: "汉🙂", count: 50_000)
        let cache = try TaskCache().consuming(page([event(1, "final_chunk", ["text": .string(long)]), event(2, "final_chunk", ["text": .string("尾"), "seq_end": .bool(true)])], after: 2), taskID: "task")
        XCTAssertLessThanOrEqual(cache.finalText.utf8.count, 256 * 1024)
        XCTAssertFalse(cache.finalText.contains("�"))
        XCTAssertTrue(cache.finalComplete)
        XCTAssertTrue(cache.finalTruncated)
        XCTAssertEqual(TaskCache.prefix("中🙂", bytes: 4), "中")
    }

    func testCancelledAndCancelRequestedAreDistinct() throws {
        var task = try JSONDecoder().decode(TaskView.self, from: Data(Self.taskJSON.utf8))
        task.status = "cancel_requested"
        XCTAssertFalse(task.terminal)
        XCTAssertTrue(task.statusLabel.contains("等待确认停止"))
        task.status = "cancelled"
        XCTAssertTrue(task.terminal)
        XCTAssertEqual(task.statusLabel, "已取消")
    }

    func testCurrentHubResponseContract() throws {
        struct Contract: Decodable {
            var nodes: NodesResponse; var projects: ProjectsResponse; var tasks: TasksResponse
            var detail: TaskView; var events: EventsResponse
        }
        let fixture = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "hub-contract", withExtension: "json"))
        let c = try JSONDecoder().decode(Contract.self, from: Data(contentsOf: fixture))
        XCTAssertTrue(c.nodes.nodes[0].online)
        XCTAssertEqual(c.detail.status, "done")
        XCTAssertTrue(c.detail.can_followup)
        var cache = TaskCache(); cache.applySnapshot(c.detail)
        cache = try cache.consuming(c.events, taskID: c.detail.id)
        XCTAssertEqual(cache.finalText, "中文兼容性验收完成")
        XCTAssertTrue(cache.finalComplete)
        XCTAssertEqual(cache.after, c.events.next_after)
    }

    func testKeychainRoundTripInIsolatedTestService() throws {
        let vault = KeychainCredentials(service: "org.example.xvis.tests.\(UUID().uuidString)")
        defer { try? vault.clear() }
        XCTAssertNil(try vault.read())
        let original = ConnectionCredential(endpoint: "https://example.com/xvis/", token: "test-only")
        let replacement = ConnectionCredential(endpoint: "https://other.example.com/xvis/", token: "replacement-test-only")
        try vault.save(original)
        XCTAssertEqual(try vault.read(), original)
        try vault.save(replacement)
        XCTAssertEqual(try vault.read(), replacement)
        try vault.clear()
        XCTAssertNil(try vault.read())
    }

    func testAPIAuthAndIdempotentBody() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        let request = PendingSubmission.newTask(nodeID: "mac-a", projectID: "accept", prompt: "测试")
        StubProtocol.handler = { urlRequest in
            XCTAssertEqual(urlRequest.url?.path, "/xvis/api/v1/tasks")
            XCTAssertNil(urlRequest.url?.query)
            XCTAssertEqual(urlRequest.value(forHTTPHeaderField: "Authorization"), "Bearer test-only")
            let body = try JSONDecoder().decode([String: String].self, from: urlRequest.httpBody ?? Data())
            XCTAssertEqual(body, request.body)
            return (200, Data("{\"task_id\":\"saved-task\",\"status\":\"queued\"}".utf8))
        }
        let api = APIClient(baseURL: try APIClient.validatedURL("https://example.com/xvis/"), token: "test-only", session: URLSession(configuration: config))
        let result: SubmissionResponse = try await api.post("api/v1/tasks", body: request.body)
        XCTAssertEqual(result.task_id, "saved-task")
    }

    func testConflictAndUnauthorizedAreReadable() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let api = APIClient(baseURL: try APIClient.validatedURL("https://example.com/xvis/"), token: "test-only", session: URLSession(configuration: config))
        for status in [401, 409] {
            StubProtocol.handler = { _ in (status, Data("{\"error\":{\"code\":\"request_id_conflict\",\"message\":\"冲突\"}}".utf8)) }
            do { let _: SubmissionResponse = try await api.post("api/v1/tasks", body: [:]); XCTFail("Expected refusal") }
            catch { XCTAssertFalse(error.localizedDescription.contains("request_id_conflict")); XCTAssertFalse(error.localizedDescription.contains("401")) }
        }
    }

    @MainActor func testLostSubmissionResponseRetriesSameRequestAfterReopen() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        let credentials = MemoryCredentials()
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: config)
        let factory: (URL, String) -> APIClient = { APIClient(baseURL: $0, token: $1, session: session) }
        var submittedBodies: [[String: String]] = []
        var loseFirstResponse = true
        StubProtocol.handler = { request in
            if request.httpMethod == "POST" {
                submittedBodies.append(try JSONDecoder().decode([String: String].self, from: request.httpBody ?? Data()))
                if loseFirstResponse { loseFirstResponse = false; throw URLError(.networkConnectionLost) }
                return (200, Data("{\"task_id\":\"one-task\",\"status\":\"queued\"}".utf8))
            }
            if request.url?.path.hasSuffix("/nodes") == true { return (200, Data("{\"nodes\":[]}".utf8)) }
            if request.url?.path.hasSuffix("/projects") == true { return (200, Data("{\"projects\":[]}".utf8)) }
            return (200, Data("{\"tasks\":[]}".utf8))
        }
        let original = AppStore(disk: disk, credentials: credentials, clientFactory: factory)
        let unconfirmed = await original.submit(nodeID: "mac-a", projectID: "accept", prompt: "仅执行一次")
        XCTAssertNil(unconfirmed)
        XCTAssertNil(original.lastConfirmedSubmission)
        XCTAssertNotNil(original.state.pending)
        let pending = original.state.pending
        let recovered = AppStore(disk: disk, credentials: credentials, clientFactory: factory)
        let confirmed = await recovered.retryPending()
        XCTAssertEqual(confirmed, pending)
        XCTAssertEqual(recovered.lastConfirmedSubmission, pending)
        XCTAssertNil(recovered.state.pending)
        XCTAssertNil(try disk.load().pending)
        XCTAssertEqual(recovered.submittedTaskID, "one-task")
        XCTAssertEqual(submittedBodies.count, 2)
        XCTAssertEqual(submittedBodies.first, submittedBodies.last)
    }

    @MainActor func testFollowupConfirmationSurvivesRefreshFailure() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: config)
        StubProtocol.handler = { request in
            if request.httpMethod == "POST" {
                XCTAssertTrue(request.url!.path.hasSuffix("/original/followup"))
                return (201, Data("{\"task_id\":\"next-task\",\"status\":\"queued\"}".utf8))
            }
            return (503, Data("{\"error\":{\"code\":\"unavailable\",\"message\":\"稍后刷新\"}}".utf8))
        }
        let store = AppStore(disk: disk, credentials: MemoryCredentials(),
                             clientFactory: { APIClient(baseURL: $0, token: $1, session: session) })
        store.errorMessage = "之前的提示仍在"
        let confirmed = await store.followup(taskID: "original", message: "下一轮原文")
        XCTAssertEqual(confirmed?.followsTaskID, "original")
        XCTAssertEqual(confirmed?.content, "下一轮原文")
        XCTAssertEqual(store.lastConfirmedSubmission, confirmed)
        XCTAssertEqual(store.submittedTaskID, "next-task")
        XCTAssertNil(try disk.load().pending)
        XCTAssertNotNil(store.connectionNote) // GET failed after the submission was confirmed.
        XCTAssertNotNil(store.errorMessage) // Confirmation must not depend on this being nil.
    }

    @MainActor func testTaskReviewFieldsAreOptionalAndListRefreshKeepsOriginalPrompt() async throws {
        let legacy = try JSONDecoder().decode(TaskView.self, from: Data(Self.taskJSON.utf8))
        XCTAssertNil(legacy.title)
        XCTAssertNil(legacy.prompt)
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let original = "写入验收文件\n第二行必须原样保留"
        var item = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(Self.taskJSON.utf8)) as? [String: Any])
        item["title"] = "写入验收文件"
        var detail = item; detail["prompt"] = original
        let listData = try JSONSerialization.data(withJSONObject: ["tasks": [item], "next_cursor": NSNull()])
        let detailData = try JSONSerialization.data(withJSONObject: detail)
        StubProtocol.handler = { request in
            let path = request.url!.path
            if path.hasSuffix("/nodes") { return (200, Data("{\"nodes\":[]}".utf8)) }
            if path.hasSuffix("/projects") { return (200, Data("{\"projects\":[]}".utf8)) }
            if path.hasSuffix("/tasks") { return (200, listData) }
            if path.hasSuffix("/events") { return (200, Data("{\"events\":[],\"next_after\":0,\"has_more\":false}".utf8)) }
            return (200, detailData)
        }
        let store = historyStore(directory: dir)
        await store.refreshVisible()
        XCTAssertEqual(store.tasks.first?.title, "写入验收文件")
        XCTAssertNil(store.tasks.first?.prompt) // The list does not carry full instructions.
        await store.openDetail("task")
        XCTAssertEqual(store.state.caches["task"]?.snapshot?.prompt, original)
        store.activeTaskID = nil // Refresh only the list after leaving details.
        await store.refreshVisible()
        XCTAssertEqual(store.tasks.first?.prompt, original)
        XCTAssertEqual(store.state.caches["task"]?.snapshot?.prompt, original)
    }

    @MainActor func testForegroundSnapshotThenEventsAndBackgroundStopsPolling() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        var state = SavedState(); state.baseURL = "https://example.com/xvis/"
        state.caches["task"] = try TaskCache().consuming(page([event(4, "text", ["text": .string("之前的步骤")])], after: 4), taskID: "task")
        try disk.save(state)
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: config)
        var detailOrder: [String] = []
        StubProtocol.handler = { request in
            let path = request.url!.path
            if path.contains("/notification-") || path.hasSuffix("/inbox") {
                return (404, Data("{\"error\":{\"code\":\"not_found\",\"message\":\"\"}}".utf8))
            }
            if path.hasSuffix("/nodes") { return (200, Data("{\"nodes\":[]}".utf8)) }
            if path.hasSuffix("/projects") { return (200, Data("{\"projects\":[]}".utf8)) }
            if path.hasSuffix("/tasks") { return (200, Data("{\"tasks\":[]}".utf8)) }
            if path.hasSuffix("/events") {
                detailOrder.append("events")
                XCTAssertTrue(request.url!.query!.contains("after=4"))
                return (200, Data("{\"events\":[],\"next_after\":4,\"has_more\":false}".utf8))
            }
            detailOrder.append("snapshot")
            return (200, Data(Self.taskJSON.utf8))
        }
        let store = AppStore(disk: disk, credentials: MemoryCredentials(), clientFactory: { APIClient(baseURL: $0, token: $1, session: session) })
        store.activeTaskID = "task"
        store.setForeground(true)
        for _ in 0..<30 {
            if detailOrder.count >= 2 { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(detailOrder, ["snapshot", "events"])
        XCTAssertTrue(store.isPolling)
        store.setForeground(false)
        XCTAssertFalse(store.isPolling)
        XCTAssertEqual(try disk.load().caches["task"]?.after, 4)
    }

    @MainActor func testHistoryRestoresMiddlePageAfterFortyBackgroundTasks() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        var total = 10
        StubProtocol.handler = Self.historyHandler { total }
        let store = historyStore(directory: dir)
        await store.refreshVisible()
        XCTAssertEqual(store.tasks.count, 10)
        XCTAssertNil(store.nextCursor)
        store.setForeground(false)
        total = 50
        await store.refreshVisible()
        XCTAssertNotNil(store.nextCursor)
        XCTAssertEqual(store.tasks.count, 40)
        await store.loadMoreTasks()
        XCTAssertEqual(store.tasks.map(\.task_id), (1...50).reversed().map { "task-\($0)" })
        XCTAssertEqual(Set(store.tasks.map(\.task_id)).count, 50)
    }

    @MainActor func testPollingDoesNotBlockThirdOrFourthHistoryPage() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        var total = 100
        StubProtocol.handler = Self.historyHandler { total }
        let store = historyStore(directory: dir)
        await store.refreshVisible()
        await store.loadMoreTasks()
        XCTAssertEqual(store.tasks.count, 60)
        await store.refreshVisible() // Same first-page polling performed every two seconds.
        await store.loadMoreTasks() // Skip the cached second page and reach the third.
        XCTAssertEqual(store.tasks.count, 90)
        await store.refreshVisible()
        await store.loadMoreTasks()
        XCTAssertEqual(store.tasks.count, 100)
        XCTAssertEqual(Set(store.tasks.map(\.task_id)).count, 100)
        total = 140 // A saved older cursor must not skip new tasks in the middle.
        await store.refreshVisible()
        await store.loadMoreTasks()
        XCTAssertEqual(store.tasks.count, 140)
        XCTAssertEqual(store.tasks.map(\.task_id), (1...140).reversed().map { "task-\($0)" })
    }

    @MainActor func testConnectionSwitchCrashNeverSendsNewCredentialToOldEndpoint() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        var previous = SavedState(); previous.baseURL = "https://server-a.example/xvis/"
        try disk.save(previous)
        let credentials = MemoryCredentials()
        // Simulate process termination after Keychain B is written but before StateDisk B.
        try credentials.save(ConnectionCredential(endpoint: "https://server-b.example/xvis/", token: "b-test-only"))
        var clientsCreated = 0
        let recovered = AppStore(disk: disk, credentials: credentials, clientFactory: { url, token in
            clientsCreated += 1
            return APIClient(baseURL: url, token: token)
        })
        XCTAssertFalse(recovered.hasConnection)
        XCTAssertTrue(recovered.errorMessage?.contains("不匹配") == true)
        recovered.setForeground(true)
        await recovered.refreshVisible()
        await recovered.openDetail("task")
        XCTAssertFalse(recovered.isPolling)
        XCTAssertEqual(clientsCreated, 0)
        XCTAssertEqual(try disk.load().baseURL, "https://server-a.example/xvis/")
        recovered.setForeground(false)
    }

    func testLegacyStateAndStepsDecodeWithMigrationDefaults() throws {
        let data = Data("""
        {"baseURL":"https://example.com/xvis/","caches":{"task":{"after":4,"steps":[{"id":4,"title":"旧步骤","text":"保留"}],"finalText":"旧回答","finalStarted":false,"finalComplete":false,"finalTruncated":false}}}
        """.utf8)
        let old = try JSONDecoder().decode(SavedState.self, from: data)
        XCTAssertEqual(old.newDraft, "")
        XCTAssertTrue(old.taskDrafts.isEmpty)
        XCTAssertEqual(old.caches["task"]?.after, 4)
        XCTAssertEqual(old.caches["task"]?.finalText, "旧回答")
        let step = try XCTUnwrap(old.caches["task"]?.steps.first)
        XCTAssertEqual(step.text, "保留")
        XCTAssertNil(step.eventType); XCTAssertNil(step.createdAt)
        XCTAssertNil(step.tool); XCTAssertNil(step.status); XCTAssertNil(step.phase)
    }

    func testStepEventsPreserveRawSemanticsWithoutGuessingSuccess() throws {
        let events = [event(1, "tool_call", ["tool": .string("read")]),
                      event(2, "tool_result", ["tool": .string("read"), "status": .string("error"), "summary": .string("读取失败")]),
                      event(3, "tool_result", ["tool": .string("bash")]),
                      event(4, "status", ["phase": .string("step_end")])]
        let cache = try TaskCache().consuming(page(events, after: 4), taskID: "task")
        XCTAssertEqual(cache.steps.map(\.eventType), ["tool_call", "tool_result", "tool_result", "status"])
        XCTAssertEqual(cache.steps[1].status, "error")
        XCTAssertEqual(cache.steps[1].tool, "read")
        XCTAssertEqual(cache.steps[1].createdAt, "2026-10-07T00:00:00Z")
        XCTAssertNil(cache.steps[2].status) // Returned without a status must not become success.
        XCTAssertEqual(cache.steps[3].phase, "step_end")
        let reopened = try JSONDecoder().decode(TaskCache.self, from: JSONEncoder().encode(cache))
        XCTAssertEqual(reopened.steps[1].status, "error")
        XCTAssertEqual(reopened.after, 4)
    }

    @MainActor func testDraftsPersistAndConfirmationClearsOnlyExactOriginal() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        StubProtocol.handler = Self.conversationHandler { [:] }
        let original = historyStore(directory: dir)
        original.setDraft("新的草稿\n第二行", for: nil)
        original.setDraft("续接草稿", for: "parent")
        original.setDraft("其他会话保留", for: "other")
        let reopened = historyStore(directory: dir)
        XCTAssertEqual(reopened.draft(for: nil), "新的草稿\n第二行")
        XCTAssertEqual(reopened.draft(for: "parent"), "续接草稿")
        let confirmed = await reopened.followup(taskID: "parent", message: "续接草稿")
        XCTAssertNotNil(confirmed)
        XCTAssertEqual(reopened.draft(for: "parent"), "")
        XCTAssertEqual(reopened.draft(for: "other"), "其他会话保留")
        XCTAssertEqual(reopened.draft(for: nil), "新的草稿\n第二行")
        XCTAssertNil(try disk.load().pending)

        var pendingState = try disk.load()
        pendingState.pending = .newTask(nodeID: "mac-a", projectID: "accept", prompt: "已经提交的旧稿")
        try disk.save(pendingState)
        let recovering = historyStore(directory: dir)
        let recovery = await recovering.retryPending()
        XCTAssertNotNil(recovery)
        XCTAssertEqual(recovering.draft(for: nil), "新的草稿\n第二行") // New edits survive an old acknowledgement.
        let matching = await recovering.submit(nodeID: "mac-a", projectID: "accept", prompt: "新的草稿\n第二行")
        XCTAssertNotNil(matching)
        XCTAssertEqual(try disk.load().newDraft, "")
    }

    @MainActor func testEndpointSwitchAndClearRemoveOnlyLocalDrafts() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        StubProtocol.handler = Self.conversationHandler { [:] }
        let store = historyStore(directory: dir)
        store.setDraft("旧服务器草稿", for: nil)
        store.setDraft("旧会话", for: "task")
        await store.connect(baseURL: "https://other.example.com/xvis/", newToken: "test-only-new")
        XCTAssertEqual(store.draft(for: nil), "")
        XCTAssertEqual(store.draft(for: "task"), "")
        store.setDraft("新的记录", for: nil)
        store.clearConnection()
        let saved = try StateDisk(directory: dir).load()
        XCTAssertEqual(saved.newDraft, "")
        XCTAssertTrue(saved.taskDrafts.isEmpty)
        XCTAssertEqual(saved.baseURL, "https://other.example.com/xvis/")
    }

    @MainActor func testFollowupAcknowledgementMovesEditedDraftWithoutOverwritingChild() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let disk = configuredTestDisk(directory: dir)
        StubProtocol.handler = Self.conversationHandler { [:] }
        var state = SavedState(); state.baseURL = "https://example.com/xvis/"
        state.pending = .followup(taskID: "parent", message: "已发送原文")
        state.taskDrafts["parent"] = "发送期间准备的下一条"
        try disk.save(state)
        let store = historyStore(directory: dir)
        let result = await store.retryPending()
        XCTAssertNotNil(result)
        let moved = try disk.load()
        XCTAssertNil(moved.pending)
        XCTAssertNil(moved.taskDrafts["parent"])
        XCTAssertEqual(moved.taskDrafts["accepted"], "发送期间准备的下一条")

        var occupied = moved
        occupied.pending = .followup(taskID: "parent", message: "另一次原文")
        occupied.taskDrafts["parent"] = "应保留的父轮次草稿"
        try disk.save(occupied)
        let reopened = historyStore(directory: dir)
        let second = await reopened.retryPending()
        XCTAssertNotNil(second)
        let retained = try disk.load()
        XCTAssertEqual(retained.taskDrafts["accepted"], "发送期间准备的下一条")
        XCTAssertEqual(retained.taskDrafts["parent"], "应保留的父轮次草稿")
        XCTAssertNil(retained.pending)
    }

    @MainActor func testConversationLoadsLinkedRoundsInOrderAndKeepsReadingSelection() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let items = try Self.conversationFixtures(count: 5)
        StubProtocol.handler = Self.conversationHandler { items }
        let store = historyStore(directory: dir)
        await store.loadConversation(containing: "round-3")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-3"), (1...5).map { "round-\($0)" })
        XCTAssertEqual(store.activeTaskID, "round-3")
        XCTAssertFalse(store.conversationLoading)
        XCTAssertNil(store.conversationNote)
        XCTAssertEqual(try StateDisk(directory: dir).load().caches.count, 5)
    }

    @MainActor func testConversationLimitIncludesCachedRoundsAndNeverFetchesBeyondTwenty() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let items = try Self.conversationFixtures(count: 26)
        var saved = SavedState(); saved.baseURL = "https://example.com/xvis/"
        for (id, data) in items {
            var cache = TaskCache(); cache.applySnapshot(try JSONDecoder().decode(TaskView.self, from: data)); saved.caches[id] = cache
        }
        try StateDisk(directory: dir).save(saved)
        var requested: [String] = []
        let handler = Self.conversationHandler { items }
        StubProtocol.handler = { request in
            if request.url?.lastPathComponent.hasPrefix("round-") == true { requested.append(request.url!.lastPathComponent) }
            return try handler(request)
        }
        let store = historyStore(directory: dir)
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1").count, 20)
        await store.loadConversation(containing: "round-1")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1"), (1...20).map { "round-\($0)" })
        XCTAssertEqual(requested.count, 20)
        XCTAssertFalse(requested.contains("round-21"))
        XCTAssertTrue(store.conversationNote?.contains("20") == true)
        XCTAssertEqual(store.conversationTaskIDs(for: "round-25"), (7...26).map { "round-\($0)" })
    }

    @MainActor func testConversationCycleStopsAndMissingPageCanRecover() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        var items = try Self.conversationFixtures(count: 2)
        let second = items.removeValue(forKey: "round-2")!
        StubProtocol.handler = Self.conversationHandler { items }
        let store = historyStore(directory: dir)
        await store.loadConversation(containing: "round-1")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1"), ["round-1"])
        XCTAssertNotNil(store.conversationNote)
        items["round-2"] = second
        await store.loadConversation(containing: "round-1")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1"), ["round-1", "round-2"])
        XCTAssertNil(store.conversationNote)

        items["round-1"] = try Self.roundFixture("round-1", previous: "round-2", following: "round-2")
        items["round-2"] = try Self.roundFixture("round-2", previous: "round-1", following: "round-1")
        await store.loadConversation(containing: "round-1")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1").count, 2)
        XCTAssertTrue(store.conversationNote?.contains("循环") == true)
    }

    @MainActor func testConversationPollingRefreshesTailAndDiscoversAcceptedFollowup() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        var items = try Self.conversationFixtures(count: 2)
        items["round-2"] = try Self.roundFixture("round-2", previous: "round-1", status: "running")
        var requested: [String] = []
        let handler = Self.conversationHandler { items }
        StubProtocol.handler = { request in
            if request.url?.lastPathComponent.hasPrefix("round-") == true { requested.append(request.url!.lastPathComponent) }
            return try handler(request)
        }
        let store = historyStore(directory: dir)
        await store.loadConversation(containing: "round-1")
        requested = []
        items["round-2"] = try Self.roundFixture("round-2", previous: "round-1")
        await store.refreshVisible()
        XCTAssertEqual(requested, ["round-2"])
        XCTAssertEqual(store.state.caches["round-2"]?.snapshot?.status, "done")
        XCTAssertTrue(store.state.caches["round-2"]?.snapshot?.can_followup == true)
        items["round-2"] = try Self.roundFixture("round-2", previous: "round-1", following: "round-3")
        items["round-3"] = try Self.roundFixture("round-3", previous: "round-2", status: "assigned")
        await store.refreshVisible()
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1"), ["round-1", "round-2", "round-3"])
        XCTAssertEqual(store.state.caches["round-2"]?.snapshot?.can_followup, false)
        XCTAssertEqual(store.state.caches["round-3"]?.snapshot?.status, "assigned")
        XCTAssertEqual(store.activeTaskID, "round-1")
    }

    @MainActor func testConversationRejectsLinkedRoundFromAnotherWorkspace() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        var items = try Self.conversationFixtures(count: 2)
        items["round-2"] = try Self.roundFixture("round-2", previous: "round-1", project: "other-project")
        StubProtocol.handler = Self.conversationHandler { items }
        let store = historyStore(directory: dir)
        await store.loadConversation(containing: "round-1")
        XCTAssertEqual(store.conversationTaskIDs(for: "round-1"), ["round-1"])
        XCTAssertNotNil(store.conversationNote)
        XCTAssertEqual(store.activeTaskID, "round-1")
    }

    private static func roundFixture(_ id: String, previous: String? = nil, following: String? = nil,
                                     status: String = "done", project: String = "accept") throws -> Data {
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(taskJSON.utf8)) as? [String: Any])
        value["task_id"] = id; value["status"] = status; value["project_id"] = project
        value["is_followup_of"] = previous; value["followup_task_id"] = following
        value["can_followup"] = status == "done" && following == nil
        value["prompt"] = "原指令 \(id)"
        if status != "done" { value.removeValue(forKey: "result") }
        return try JSONSerialization.data(withJSONObject: value)
    }

    private static func conversationFixtures(count: Int) throws -> [String: Data] {
        var result: [String: Data] = [:]
        for n in 1...count {
            let id = "round-\(n)"
            result[id] = try roundFixture(id, previous: n > 1 ? "round-\(n - 1)" : nil,
                                          following: n < count ? "round-\(n + 1)" : nil)
        }
        return result
    }

    private static func conversationHandler(items: @escaping () -> [String: Data]) -> (URLRequest) throws -> (Int, Data) {
        { request in
            let component = request.url!.lastPathComponent
            if request.httpMethod == "POST" { return (201, Data("{\"task_id\":\"accepted\",\"status\":\"queued\"}".utf8)) }
            if component == "nodes" { return (200, Data("{\"nodes\":[]}".utf8)) }
            if component == "projects" { return (200, Data("{\"projects\":[]}".utf8)) }
            if component == "tasks" { return (200, Data("{\"tasks\":[]}".utf8)) }
            if component == "events" {
                let after = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "after" }?.value ?? "0"
                return (200, Data("{\"events\":[],\"next_after\":\(after),\"has_more\":false}".utf8))
            }
            if let data = items()[component] { return (200, data) }
            return (404, Data("{\"error\":{\"code\":\"not_found\",\"message\":\"记录暂不可用\"}}".utf8))
        }
    }

    @MainActor private func historyStore(directory: URL) -> AppStore {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: config)
        return AppStore(disk: configuredTestDisk(directory: directory), credentials: MemoryCredentials(),
                        clientFactory: { APIClient(baseURL: $0, token: $1, session: session) })
    }

    private static func historyHandler(total: @escaping () -> Int) -> (URLRequest) throws -> (Int, Data) {
        { request in
            let path = request.url!.path
            if path.hasSuffix("/nodes") { return (200, Data("{\"nodes\":[]}".utf8)) }
            if path.hasSuffix("/projects") { return (200, Data("{\"projects\":[]}".utf8)) }
            let count = total()
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            let before = components.queryItems?.first { $0.name == "cursor" }?.value.flatMap(Int.init) ?? count + 1
            let top = min(count, before - 1)
            let bottom = max(1, top - 29)
            let values: [[String: Any]] = top >= bottom ? (bottom...top).reversed().map { n in
                ["task_id": "task-\(n)", "status": "queued", "node_id": "mac-a", "project_id": "accept",
                 "created_at": String(format: "2026-10-07T00:%02d:%02dZ", n / 60, n % 60), "can_followup": false]
            } : []
            let response: [String: Any] = ["tasks": values, "next_cursor": bottom > 1 ? String(bottom) : NSNull()]
            return (200, try JSONSerialization.data(withJSONObject: response))
        }
    }

    private func event(_ id: Int, _ type: String, _ payload: [String: JSONValue]) -> EventView {
        EventView(id: id, task_id: "task", type: type, payload: payload, created_at: "2026-10-07T00:00:00Z")
    }
    private func page(_ events: [EventView], after: Int, more: Bool = false) -> EventsResponse {
        EventsResponse(events: events, next_after: after, has_more: more)
    }
    private static let taskJSON = """
    {"task_id":"task","status":"done","node_id":"mac-a","project_id":"accept","created_at":"2026-10-07T00:00:00Z","result":{"final_text":"回答","truncated":false},"can_followup":true}
    """
}

private final class MemoryCredentials: CredentialStore {
    var value: ConnectionCredential? = ConnectionCredential(endpoint: "https://example.com/xvis/", token: "test-only")
    func read() throws -> ConnectionCredential? { value }
    func save(_ credential: ConnectionCredential) throws { value = credential }
    func clear() throws { value = nil }
}

private final class StubProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, Data))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            guard let handler = Self.handler else { throw AppFailure.protocolMismatch }
            var r = request
            if r.httpBody == nil, let stream = r.httpBodyStream {
                stream.open(); defer { stream.close() }
                var body = Data(); let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: 4096)
                defer { buffer.deallocate() }
                while stream.hasBytesAvailable { let count = stream.read(buffer, maxLength: 4096); if count <= 0 { break }; body.append(buffer, count: count) }
                r.httpBody = body
            }
            let (status, data) = try handler(r)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: r.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() { }
}

// Network fixtures explicitly opt into a synthetic endpoint; production defaults stay blank.
private func configuredTestDisk(directory: URL) -> StateDisk {
    let disk = StateDisk(directory: directory)
    do {
        var state = try disk.load()
        if state.baseURL.isEmpty {
            state.baseURL = "https://example.com/xvis/"
            try disk.save(state)
        }
    } catch { XCTFail("Cannot prepare isolated connection fixture: \(error)") }
    return disk
}
