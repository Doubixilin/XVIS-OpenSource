import XCTest
@testable import XVIS

final class InteractionTests: XCTestCase {
    func testPairingLinkRequiresSameHubAndIndependentDeviceScope() throws {
        let base = try APIClient.validatedURL("https://example.test/xvis/")
        let rid = "p-" + String(repeating: "a", count: 32)
        let secret = String(repeating: "b", count: 43)
        var url = URLComponents(string: "xvis-pair://device")!
        url.queryItems = [.init(name: "server", value: base.absoluteString), .init(name: "id", value: rid), .init(name: "secret", value: secret)]
        XCTAssertEqual(try PairingLink(url.string!, expectedServer: base).requestID, rid)
        XCTAssertThrowsError(try PairingLink(url.string!, expectedServer: APIClient.validatedURL("https://other.test/")))
        url.queryItems!.append(.init(name: "id", value: rid))
        XCTAssertThrowsError(try PairingLink(url.string!, expectedServer: base))
        let first = ProjectView(project_id: "p-xvis-test", name: "原设备", node_id: "mac-a", available: false)
        let second = ProjectView(project_id: "p-xvis-test", name: "第二设备", node_id: "n-second", available: true)
        XCTAssertNotEqual(first.scopedID, second.scopedID)
        XCTAssertNil(ProjectView.defaultWorkspace(in: [first, second]))
    }
    func testIncrementalMarkdownMatchesFullParserAtEveryCharacter() {
        let sources = ["# 标题\n\n第一段中文🙂。\n\n第二段\n- 条目\n\n尾部", "前文\n\n```swift\nlet x = 1\n\nprint(x)\n```\n\n尾部",
            "|列一|列二|\n|---|---|\n|中|文|\n\n后文", "\n\n开头\n\n\n结尾\n", "> 一行\n> 二行\n\n尾部", "开始\r\n\r\n~~~\r\n代码\r\n~~~\r\n\r\n最后"]
        for source in sources {
            let cache = MarkdownIncrementalCache()
            for count in 0...source.count {
                let partial = String(source.prefix(count))
                XCTAssertEqual(cache.blocks(for: partial), MarkdownAnswerParser.parse(partial), partial)
            }
            XCTAssertGreaterThan(cache.reusedPrefixCount, 0)
            for replacement in ["", "改写\n\n不同尾部", source, String(source.prefix(5))] {
                XCTAssertEqual(cache.blocks(for: replacement), MarkdownAnswerParser.parse(replacement))
            }
        }
    }
    func testInteractionPushMutuallyExclusiveRoutesAndLegacyDecode() throws {
        let server = "s-" + String(repeating: "a", count: 32)
        let q = "q-" + String(repeating: "b", count: 32)
        let value: [String: Any] = ["server_id": server, "task_id": "t-test", "interaction_id": q, "revision": 1]
        XCTAssertNotNil(XNotificationPayload.parse(["xvis": value]))
        var both = value; both["result_id"] = "r-" + String(repeating: "c", count: 32)
        XCTAssertNil(XNotificationPayload.parse(["xvis": both]))
        var bad = value; bad["interaction_id"] = "../other"
        XCTAssertNil(XNotificationPayload.parse(["xvis": bad]))
        var revision = value; revision["revision"] = 2
        XCTAssertNil(XNotificationPayload.parse(["xvis": revision]))
    }
    func testProvisionalStreamResetsAndFinalAlwaysWins() throws {
        let raw: [[String: Any]] = [["id": 1, "type": "answer_delta", "payload": ["text": "旧尝试"]],
            ["id": 2, "type": "answer_reset", "payload": [:]], ["id": 3, "type": "answer_delta", "payload": ["text": "正文"]],
            ["id": 4, "type": "final", "payload": ["text": "权威最终正文"]], ["id": 5, "type": "answer_delta", "payload": ["text": "过时"]]]
        let events = try raw.map { value -> EventView in
            var value = value; value["task_id"] = "t-test"; value["created_at"] = "2026-10-10T00:00:00Z"
            return try JSONDecoder().decode(EventView.self, from: JSONSerialization.data(withJSONObject: value))
        }
        let partial = try TaskCache().consuming(.init(events: Array(events.prefix(3)), next_after: 3, has_more: false), taskID: "t-test")
        XCTAssertEqual(partial.finalText, "正文"); XCTAssertFalse(partial.finalComplete)
        let final = try partial.consuming(.init(events: Array(events.suffix(2)), next_after: 5, has_more: false), taskID: "t-test")
        XCTAssertEqual(final.finalText, "权威最终正文"); XCTAssertTrue(final.finalComplete)
    }
    @MainActor func testLostControlResponseKeepsOriginalContentAndIdentity() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory); H1Protocol.handler = nil }
        let disk = StateDisk(directory: directory)
        var configured = SavedState(); configured.baseURL = "https://example.com/xvis/"
        try disk.save(configured)
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [H1Protocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        var requests: [ControlCommand] = []
        H1Protocol.handler = { request in
            let value = try JSONDecoder().decode(ControlCommand.self, from: request.httpBody!)
            requests.append(value)
            XCTAssertEqual(try disk.load().controlSubmissions["t-test"]?.id, value.id)
            throw URLError(.networkConnectionLost)
        }
        func store() -> AppStore { AppStore(disk: disk, credentials: H1Credentials(), clientFactory: { APIClient(baseURL: $0, token: $1, session: session) }) }
        let command = ControlCommand(epoch: UUID().uuidString, action: "queue", text: "原始补充")
        do { try await store().submitControl(taskID: "t-test", command: command); XCTFail() } catch { }
        do { try await store().submitControl(taskID: "t-test", command: .init(epoch: command.epoch, action: "steer", text: "不同内容")); XCTFail() } catch { }
        XCTAssertEqual(requests.count, 2)
        guard requests.count == 2 else { return }
        XCTAssertEqual(requests[0].id, requests[1].id)
        XCTAssertEqual(requests[1].text, "原始补充"); XCTAssertEqual(requests[1].action, "queue")
    }
    private func item() throws -> TaskInteraction {
        let value: [String: Any] = ["interaction_id": "q-" + String(repeating: "a", count: 32), "task_id": "t-test",
            "revision": 1, "kind": "question", "epoch": "epoch", "session_id": "session-test", "call_id": "call-test",
            "expires_at": "2099-01-01T00:00:00Z", "status": "pending",
            "request": ["questions": [["id": "format", "question": "选择格式", "options": [["label": "CSV"], ["label": "MD"]]]]]]
        return try JSONDecoder().decode(TaskInteraction.self, from: JSONSerialization.data(withJSONObject: value))
    }

    func testInteractionStatesAndExpiryFailClosed() throws {
        var i = try item()
        XCTAssertTrue(i.canRespond)
        for status in ["submitted", "confirmed", "expired", "cancelled", "interrupted", "unknown"] {
            i.status = status
            XCTAssertFalse(i.canRespond)
        }
        i.status = "pending"; i.expires_at = "2000-01-01T00:00:00Z"
        XCTAssertFalse(i.canRespond)
        i.expires_at = "invalid"; XCTAssertFalse(i.isValid)
        i = try item(); i.task_id = "../foreign"; XCTAssertFalse(i.isValid)
    }

    func testSingleMultiAndFreeTextAnswerValidation() throws {
        var q = try item().request.questions!
        XCTAssertNil(InteractionAnswer.value(questions: q, selected: [:], custom: [:]))
        XCTAssertNil(InteractionAnswer.value(questions: q, selected: ["format": ["CSV", "MD"]], custom: [:]))
        XCTAssertNil(InteractionAnswer.value(questions: q, selected: ["format": ["bad"]], custom: [:]))
        XCTAssertNil(InteractionAnswer.value(questions: q, selected: [:], custom: ["format": String(repeating: "中", count: 700)]))
        q[0].multiSelect = true
        XCTAssertNotNil(InteractionAnswer.value(questions: q, selected: ["format": ["CSV", "MD"]], custom: ["format": "日期排序"]))
        XCTAssertNotNil(InteractionAnswer.value(questions: q, selected: [:], custom: ["format": "纯文字"]))
    }

    func testLegacyCacheAndDurableAnswerRoundTrip() throws {
        var state = try JSONDecoder().decode(SavedState.self, from: Data("{}".utf8))
        XCTAssertTrue(state.interactionSubmissions.isEmpty)
        let response = InteractionSubmission(revision: 1, value: .string("rejected"))
        state.interactionSubmissions["q-test"] = response
        let restored = try JSONDecoder().decode(SavedState.self, from: JSONEncoder().encode(state))
        XCTAssertEqual(restored.interactionSubmissions["q-test"]?.response_id, response.response_id)
        XCTAssertEqual(restored.interactionSubmissions["q-test"]?.value, .string("rejected"))
    }

    @MainActor func testLostResponseReopenRetriesOriginalDecisionAndID() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory); H1Protocol.handler = nil }
        let disk = StateDisk(directory: directory)
        var configured = SavedState(); configured.baseURL = "https://example.com/xvis/"
        try disk.save(configured)
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [H1Protocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        var requests: [InteractionSubmission] = []
        H1Protocol.handler = { request in
            if request.httpMethod == "POST" {
                let response = try JSONDecoder().decode(InteractionSubmission.self, from: request.httpBody!)
                requests.append(response)
                // The local outbox must be durable before any effective HTTP operation.
                XCTAssertEqual(try disk.load().interactionSubmissions.values.first?.response_id, response.response_id)
                throw URLError(.networkConnectionLost)
            }
            throw URLError(.notConnectedToInternet)
        }
        func store() -> AppStore {
            AppStore(disk: disk, credentials: H1Credentials(), clientFactory: { APIClient(baseURL: $0, token: $1, session: session) })
        }
        var i = try item(); i.kind = "approval"
        i.request = InteractionRequest(tool_name: "bash", reason: "test", arguments: .object(["command": .string("printf test")]))
        do { try await store().respondToInteraction(i, value: .string("rejected")); XCTFail() } catch { }
        do { try await store().respondToInteraction(i, value: .string("allowed-once")); XCTFail() } catch { }
        XCTAssertEqual(requests.count, 2)
        guard requests.count == 2 else { return }
        XCTAssertEqual(requests[0].response_id, requests[1].response_id)
        XCTAssertEqual(requests[1].value, .string("rejected"))
    }

    @MainActor func testExpiredInteractionNeverWritesAnAnswer() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let disk = StateDisk(directory: directory)
        var configured = SavedState(); configured.baseURL = "https://example.com/xvis/"
        try disk.save(configured)
        let store = AppStore(disk: disk, credentials: H1Credentials())
        var i = try item(); i.expires_at = "2000-01-01T00:00:00Z"
        do { try await store.respondToInteraction(i, value: .string("allowed-once")); XCTFail() } catch { }
        XCTAssertTrue(try disk.load().interactionSubmissions.isEmpty)
    }
}

private final class H1Credentials: CredentialStore {
    func read() throws -> ConnectionCredential? { ConnectionCredential(endpoint: "https://example.com/xvis/", token: "test-only") }
    func save(_ credential: ConnectionCredential) throws { }
    func clear() throws { }
}

private final class H1Protocol: URLProtocol {
    static var handler: ((URLRequest) throws -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            var request = request
            if request.httpBody == nil, let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var data = Data(); var bytes = [UInt8](repeating: 0, count: 4096)
                while stream.hasBytesAvailable { let count = stream.read(&bytes, maxLength: bytes.count); if count <= 0 { break }; data.append(bytes, count: count) }
                request.httpBody = data
            }
            try Self.handler?(request)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() { }
}
