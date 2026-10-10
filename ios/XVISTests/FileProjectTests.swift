import XCTest
@testable import XVIS

final class FileProjectTests: XCTestCase {
    func testLegacyTasksAndStateDoNotRequireFilesOrProjectContext() throws {
        let task = try JSONDecoder().decode(TaskView.self, from: FFixture.taskData())
        XCTAssertNil(task.output_files); XCTAssertNil(task.file_transfer); XCTAssertNil(task.project_context)
        let old = try JSONDecoder().decode(SavedState.self, from: Data("{}".utf8))
        XCTAssertTrue(old.projectContexts.isEmpty); XCTAssertTrue(old.projectEditDrafts.isEmpty)
        var value = try FFixture.taskObject()
        value["project_context"] = [:]
        let empty = try JSONDecoder().decode(TaskView.self, from: JSONSerialization.data(withJSONObject: value))
        XCTAssertEqual(empty.project_context?.revision, 0)
    }

    func testFileMetadataRejectsPathsMismatchedTypesAndOversize() {
        let valid = FFixture.file(Data("文件内容".utf8))
        XCTAssertTrue(valid.isValid)
        for name in ["../报告.txt", "文件/报告.txt", "文件\\报告.txt", ".", "..", "bad\n.txt"] {
            var bad = valid; bad.filename = name; XCTAssertFalse(bad.isValid)
        }
        var bad = valid; bad.mime_type = "application/pdf"; XCTAssertFalse(bad.isValid)
        bad = valid; bad.size_bytes = FileDisk.byteLimit + 1; XCTAssertFalse(bad.isValid)
        bad = valid; bad.sha256 = String(repeating: "z", count: 64); XCTAssertFalse(bad.isValid)
    }

    func testVerifiedFileCachePreservesExactBytesAndEndpointScope() throws {
        let fixture = try FFixture { _ in (500, Data()) }; defer { fixture.cleanUp() }
        let bytes = Data("XVIS 文件验收\n记录保持原样。".utf8)
        let file = FFixture.file(bytes)
        let source = fixture.directory.appendingPathComponent("download")
        try bytes.write(to: source)
        let disk = FileDisk(directory: fixture.directory)
        let installed = try disk.install(source, file: file, endpoint: fixture.url.absoluteString)
        XCTAssertEqual(try Data(contentsOf: installed), bytes)
        XCTAssertEqual(installed.lastPathComponent, file.filename)
        XCTAssertEqual(try disk.cached(file, endpoint: fixture.url.absoluteString), installed)
        XCTAssertNil(try disk.cached(file, endpoint: "https://another.example/xvis/"))
    }

    func testWrongLengthAndHashAreRejectedBeforeCacheInstallation() throws {
        let fixture = try FFixture { _ in (500, Data()) }; defer { fixture.cleanUp() }
        let bytes = Data("原始内容".utf8)
        let file = FFixture.file(bytes)
        let disk = FileDisk(directory: fixture.directory)
        let source = fixture.directory.appendingPathComponent("bad")
        try Data("其他内容".utf8).write(to: source)
        XCTAssertThrowsError(try disk.install(source, file: file, endpoint: fixture.url.absoluteString))
        XCTAssertNil(try disk.cached(file, endpoint: fixture.url.absoluteString))
        try (bytes + Data([0])).write(to: source)
        XCTAssertThrowsError(try disk.install(source, file: file, endpoint: fixture.url.absoluteString))
    }

    func testCorruptCachedFileIsRemovedRatherThanPresented() throws {
        let fixture = try FFixture { _ in (500, Data()) }; defer { fixture.cleanUp() }
        let bytes = Data("校验成功".utf8); let file = FFixture.file(bytes)
        let source = fixture.directory.appendingPathComponent("download"); try bytes.write(to: source)
        let disk = FileDisk(directory: fixture.directory)
        let cached = try disk.install(source, file: file, endpoint: fixture.url.absoluteString)
        try Data("修改后的文件".utf8).write(to: cached)
        XCTAssertNil(try disk.cached(file, endpoint: fixture.url.absoluteString))
        XCTAssertFalse(FileManager.default.fileExists(atPath: cached.path))
    }

    func testFileCacheEvictsOlderFilesAtItsBudgetAndRetainsTheActiveFile() throws {
        let fixture = try FFixture { _ in (500, Data()) }; defer { fixture.cleanUp() }
        let disk = FileDisk(directory: fixture.directory)
        let bytes = Data(repeating: 65, count: FileDisk.byteLimit)
        let source = fixture.directory.appendingPathComponent("large-download"); try bytes.write(to: source)
        var first: OutputFile?
        var last: OutputFile?
        for index in 1...9 {
            var file = FFixture.file(bytes); file.file_id = "f-" + String(format: "%032x", index)
            if index == 1 { first = file }; last = file
            _ = try disk.install(source, file: file, endpoint: fixture.url.absoluteString)
        }
        XCTAssertNil(try disk.cached(XCTUnwrap(first), endpoint: fixture.url.absoluteString))
        XCTAssertNotNil(try disk.cached(XCTUnwrap(last), endpoint: fixture.url.absoluteString))
        let enumerator = try XCTUnwrap(FileManager.default.enumerator(at: disk.directory, includingPropertiesForKeys: [.fileSizeKey, .isRegularFileKey]))
        var total = 0
        for case let url as URL in enumerator {
            let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            if values.isRegularFile == true { total += values.fileSize ?? 0 }
        }
        XCTAssertLessThanOrEqual(total, FileDisk.cacheLimit)
    }

    @MainActor func testRefreshingDeliveryDoesNotChangeTaskCompletionOrCreateModelTask() async throws {
        let bytes = Data("交付内容".utf8); let file = FFixture.file(bytes)
        var calls: [String] = []
        let fixture = try FFixture { request in
            calls.append(request.url!.path)
            return (200, try FFixture.json(["files": [try FFixture.object(file)], "transfer_state": "ready", "note": NSNull(), "retry_requested": false]))
        }; defer { fixture.cleanUp() }
        var state = try fixture.disk.load(); state.caches["round-1"] = TaskCache(snapshot: try FFixture.task())
        try fixture.disk.save(state)
        let store = fixture.store()
        let response = try await store.refreshTaskFiles("round-1")
        XCTAssertEqual(response.files, [file]); XCTAssertEqual(store.state.caches["round-1"]?.snapshot?.status, "done")
        XCTAssertEqual(store.state.caches["round-1"]?.snapshot?.file_transfer?.state, "ready")
        XCTAssertEqual(calls, ["/xvis/api/v1/tasks/round-1/files"])
        XCTAssertNil(store.state.pending)
    }

    @MainActor func testFileRetryCallsOnlySupplementaryTransferRoutes() async throws {
        var calls: [String] = []
        let fixture = try FFixture { request in
            calls.append(request.httpMethod! + " " + request.url!.path)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture-only")
            if request.httpMethod == "POST" { return (200, try FFixture.json(["recorded": true, "node_online": false])) }
            return (200, try FFixture.json(["files": [], "transfer_state": "failed", "note": "待补传", "retry_requested": true]))
        }; defer { fixture.cleanUp() }
        let reply = try await fixture.store().retryFileDelivery(taskID: "round-1")
        XCTAssertTrue(reply.recorded); XCTAssertFalse(reply.node_online)
        XCTAssertEqual(calls, ["POST /xvis/api/v1/tasks/round-1/files/retry", "GET /xvis/api/v1/tasks/round-1/files"])
    }

    func testPendingFilesAndUnsafeTaskIDsCannotStartDownloads() async throws {
        var count = 0
        let fixture = try FFixture { _ in count += 1; return (500, Data()) }; defer { fixture.cleanUp() }
        let api = APIClient(baseURL: fixture.url, token: "fixture-only", session: fixture.session)
        var file = FFixture.file(Data("内容".utf8)); file.status = "pending"
        do { _ = try await api.fileDownload(file, taskID: "round-1"); XCTFail() } catch AppFailure.protocolMismatch { }
        file.status = "ready"
        do { _ = try await api.fileDownload(file, taskID: "../another"); XCTFail() } catch AppFailure.protocolMismatch { }
        XCTAssertEqual(count, 0)
    }

    func testProjectLimitsCountUTF8AndTotalPayload() {
        var context = ProjectContext(); context.goal = String(repeating: "中", count: 1366)
        XCTAssertNotNil(context.validationMessage)
        context.goal = String(repeating: "a", count: 4096); XCTAssertNil(context.validationMessage)
        context.constraints = String(repeating: "a", count: 4096)
        context.materials = String(repeating: "a", count: 4096)
        XCTAssertNotNil(context.validationMessage)
        context = ProjectContext(); context.source_task_ids = ["same", "same"]
        XCTAssertNotNil(context.validationMessage)
    }

    @MainActor func testProjectDraftReopensWithoutMixingProjects() throws {
        let fixture = try FFixture { _ in (500, Data()) }; defer { fixture.cleanUp() }
        let store = fixture.store()
        var a = ProjectContext(); a.goal = "项目甲独有目标"; a.progress = "尚未保存"
        let draft = ProjectEditDraft(context: a, baseline: ProjectContext().editable, summaryRequestID: "request-fixed",
                                     summarySourceIDs: ["round-1"], summaryTaskID: nil)
        try store.persistProjectEditDraft(draft, nodeID: "mac-a", projectID: "project-a")
        let reopened = fixture.store()
        XCTAssertEqual(reopened.projectEditDraft(nodeID: "mac-a", projectID: "project-a"), draft)
        XCTAssertNil(reopened.projectEditDraft(nodeID: "mac-a", projectID: "project-b"))
        XCTAssertNil(reopened.projectEditDraft(nodeID: "mac-b", projectID: "project-a"))
        XCTAssertTrue(reopened.state.projectContexts.isEmpty)
    }

    @MainActor func testProjectSaveSendsOnlyEditableFieldsAndKeepsOldTaskSnapshot() async throws {
        var submitted: [String: Any]?
        let fixture = try FFixture { request in
            submitted = try FFixture.body(request)
            var saved = FFixture.context(revision: 2); saved.goal = "新目标"
            return (200, try JSONEncoder().encode(saved))
        }; defer { fixture.cleanUp() }
        var state = try fixture.disk.load()
        var oldTask = try FFixture.task(); oldTask.project_context = FFixture.context(revision: 1); oldTask.project_context_revision = 1
        state.caches[oldTask.id] = TaskCache(snapshot: oldTask); try fixture.disk.save(state)
        let store = fixture.store()
        var draft = FFixture.context(revision: 1); draft.goal = "新目标"
        try store.persistProjectEditDraft(ProjectEditDraft(context: draft, baseline: FFixture.context(revision: 1).editable), nodeID: "mac-a", projectID: "project-a")
        let saved = try await store.saveProjectContext(draft, nodeID: "mac-a", projectID: "project-a")
        XCTAssertEqual(saved.revision, 2)
        XCTAssertEqual(Set(try XCTUnwrap(submitted).keys), Set(["revision", "goal", "constraints", "materials", "decisions", "progress", "open_questions", "next_steps", "source_task_ids"]))
        XCTAssertEqual(store.state.caches[oldTask.id]?.snapshot?.project_context?.goal, "原目标")
        XCTAssertEqual(store.state.caches[oldTask.id]?.snapshot?.project_context_revision, 1)
        XCTAssertNil(store.projectEditDraft(nodeID: "mac-a", projectID: "project-a"))
    }

    @MainActor func testProjectConflictPreservesDraftAndCachedBaseline() async throws {
        let fixture = try FFixture { _ in (409, try FFixture.json(["error": ["code": "project_context_conflict", "message": "conflict"]])) }; defer { fixture.cleanUp() }
        let store = fixture.store(); var draft = FFixture.context(revision: 1); draft.progress = "我的未保存进度"
        let edit = ProjectEditDraft(context: draft, baseline: FFixture.context(revision: 1).editable)
        try store.persistProjectEditDraft(edit, nodeID: "mac-a", projectID: "project-a")
        do { _ = try await store.saveProjectContext(draft, nodeID: "mac-a", projectID: "project-a"); XCTFail() }
        catch { XCTAssertTrue(error.localizedDescription.contains("编辑稿已保留")) }
        XCTAssertEqual(store.projectEditDraft(nodeID: "mac-a", projectID: "project-a"), edit)
        XCTAssertEqual(try fixture.disk.load().projectEditDrafts["mac-a/project-a"], edit)
        XCTAssertTrue(store.state.projectContexts.isEmpty)
    }

    @MainActor func testWrongProjectResponseIsRejectedAndNeverStored() async throws {
        let fixture = try FFixture { _ in
            var wrong = FFixture.context(revision: 1); wrong.project_id = "project-b"
            return (200, try JSONEncoder().encode(wrong))
        }; defer { fixture.cleanUp() }
        let store = fixture.store()
        do { _ = try await store.fetchProjectContext(nodeID: "mac-a", projectID: "project-a"); XCTFail() }
        catch AppFailure.protocolMismatch { }
        XCTAssertTrue(store.state.projectContexts.isEmpty)
    }

    @MainActor func testSourceSearchIsFixedToSelectedScopeAndRetainsTruncation() async throws {
        let source = ProjectSource(task_id: "round-1", title: "原任务", status: "done", created_at: "2026-10-09T00:00:00Z",
                                   finished_at: nil, prompt: "原指令", text: "摘录", truncated: true)
        let fixture = try FFixture { request in
            XCTAssertEqual(request.url?.path, "/xvis/api/v1/nodes/mac-a/projects/project-a/context/sources")
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
            XCTAssertEqual(query?.first(where: { $0.name == "query" })?.value, "核对决定")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture-only")
            return (200, try FFixture.json(["sources": [try FFixture.object(source)]]))
        }; defer { fixture.cleanUp() }
        let results = try await fixture.store().projectSources(nodeID: "mac-a", projectID: "project-a", query: "核对决定")
        XCTAssertEqual(results, [source]); XCTAssertTrue(results[0].truncated)
    }

    @MainActor func testSummaryRequestUsesFrozenIDAndSourcesWithoutChangingChatDraft() async throws {
        var payloads: [[String: Any]] = []
        let fixture = try FFixture { request in
            XCTAssertEqual(request.url?.path, "/xvis/api/v1/nodes/mac-a/projects/project-a/context/summary")
            payloads.append(try FFixture.body(request))
            return (200, try FFixture.json(["task_id": "summary-1", "status": "queued"]))
        }; defer { fixture.cleanUp() }
        let store = fixture.store(); store.setDraft("普通聊天草稿", for: nil)
        for _ in 0..<2 {
            _ = try await store.createProjectSummary(nodeID: "mac-a", projectID: "project-a", requestID: "same-id", sourceIDs: ["round-1", "round-2"])
        }
        XCTAssertEqual(payloads.count, 2)
        XCTAssertEqual(payloads[0]["client_request_id"] as? String, "same-id")
        XCTAssertEqual(payloads[0]["source_task_ids"] as? [String], ["round-1", "round-2"])
        XCTAssertEqual(try FFixture.json(payloads[0]), try FFixture.json(payloads[1]))
        XCTAssertEqual(store.draft(for: nil), "普通聊天草稿"); XCTAssertNil(store.state.pending)
    }

    @MainActor func testOldHubMissingContextDoesNotPoisonChatConnection() async throws {
        let fixture = try FFixture { _ in (404, try FFixture.json(["detail": "Not Found"])) }; defer { fixture.cleanUp() }
        let store = fixture.store()
        do { _ = try await store.fetchProjectContext(nodeID: "mac-a", projectID: "project-a"); XCTFail() }
        catch { XCTAssertTrue(error.localizedDescription.contains("聊天仍可继续")) }
        XCTAssertTrue(store.hasConnection); XCTAssertFalse(store.connectionFailed); XCTAssertNil(store.errorMessage)
    }

    @MainActor func testCrossProjectTaskSnapshotIsNotAcceptedIntoConversationCache() async throws {
        let fixture = try FFixture { _ in
            var task = try FFixture.task(); task.project_context = FFixture.context(revision: 1)
            task.project_context?.project_id = "project-b"
            return (200, try JSONEncoder().encode(task))
        }; defer { fixture.cleanUp() }
        let store = fixture.store()
        await store.loadConversation(containing: "round-1")
        XCTAssertNil(store.state.caches["round-1"]?.snapshot)
        XCTAssertNotNil(store.errorMessage)
    }
}

private final class FFixture {
    let directory: URL
    let disk: StateDisk
    let url: URL
    let session: URLSession
    private let host: String
    init(handler: @escaping (URLRequest) throws -> (Int, Data)) throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("XVIS-file-project-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        disk = StateDisk(directory: directory)
        host = UUID().uuidString.lowercased() + ".example.com"
        url = URL(string: "https://\(host)/xvis/")!
        var state = SavedState(); state.baseURL = url.absoluteString; try disk.save(state)
        FProtocol.register(host, handler)
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [FProtocol.self]
        session = URLSession(configuration: config)
    }
    @MainActor func store() -> AppStore {
        let session = self.session
        return AppStore(disk: disk, credentials: FCredentials(endpoint: url.absoluteString),
                        clientFactory: { APIClient(baseURL: $0, token: $1, session: session) })
    }
    func cleanUp() { session.invalidateAndCancel(); FProtocol.remove(host); try? FileManager.default.removeItem(at: directory) }
    static func json(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
    static func object<T: Encodable>(_ value: T) throws -> Any { try JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) }
    static func body(_ request: URLRequest) throws -> [String: Any] {
        var bytes = request.httpBody ?? Data()
        if bytes.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }; var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let n = stream.read(&buffer, maxLength: buffer.count); if n <= 0 { break }; bytes.append(contentsOf: buffer.prefix(n)) }
        }
        return try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
    }
    static func taskObject() throws -> [String: Any] {
        ["task_id": "round-1", "status": "done", "node_id": "mac-a", "project_id": "project-a", "created_at": "2026-10-09T00:00:00Z", "can_followup": true]
    }
    static func taskData() throws -> Data { try json(taskObject()) }
    static func task() throws -> TaskView { try JSONDecoder().decode(TaskView.self, from: taskData()) }
    static func file(_ data: Data) -> OutputFile {
        OutputFile(file_id: "f-" + String(repeating: "a", count: 32), filename: "成果.txt", mime_type: "text/plain", size_bytes: data.count,
                   sha256: ImageDisk.digest(data), status: "ready", error: nil)
    }
    static func context(revision: Int) -> ProjectContext {
        var value = ProjectContext(); value.schema_version = 1; value.revision = revision
        value.node_id = "mac-a"; value.project_id = "project-a"; value.goal = "原目标"
        return value
    }
}
private struct FCredentials: CredentialStore {
    let endpoint: String
    func read() throws -> ConnectionCredential? { ConnectionCredential(endpoint: endpoint, token: "fixture-only") }
    func save(_ credential: ConnectionCredential) throws { }
    func clear() throws { }
}
private final class FProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> (Int, Data)] = [:]
    static func register(_ host: String, _ handler: @escaping (URLRequest) throws -> (Int, Data)) { lock.lock(); defer { lock.unlock() }; handlers[host] = handler }
    static func remove(_ host: String) { lock.lock(); defer { lock.unlock() }; handlers.removeValue(forKey: host) }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); let handler = Self.handlers[request.url!.host!]; Self.lock.unlock()
        do {
            let (status, bytes) = try XCTUnwrap(handler)(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: bytes); client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() { }
}
