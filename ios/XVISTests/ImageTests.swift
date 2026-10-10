import XCTest
import UIKit
@testable import XVIS

final class ImageTests: XCTestCase {
    @MainActor func testUnconfirmedImageSubmissionReopensWithTheSameRequestIDsAndBytes() async throws {
        let capture = XImageRequestCapture()
        let fixture = try XImageTestHarness { request in
            if request.httpMethod == "PUT" {
                let data = try XCTUnwrap(request.httpBody)
                let id = try XCTUnwrap(request.url?.lastPathComponent)
                capture.recordUpload(id: id, data: data)
                return .data(200, try JSONEncoder().encode(Self.metadata(data, id: id)))
            }
            if request.httpMethod == "POST" {
                capture.recordPost(try Self.body(request))
                if capture.takeFirstResponseLoss() { return .failure(URLError(.networkConnectionLost)) }
                return Self.accepted
            }
            return Self.listReply(request)
        }
        defer { fixture.cleanUp() }
        let bytes = try imageBytes(.systemTeal)
        let original = fixture.store()
        original.setDraft("请分析附图\n保留原文", for: nil)
        try original.addImage(data: bytes, for: nil)
        let drafts = original.imageDrafts(for: nil)
        let first = await original.submit(nodeID: "mac-a", projectID: "accept", prompt: original.draft(for: nil))
        XCTAssertNil(first)
        let pending = try XCTUnwrap(original.state.pending)
        XCTAssertEqual(pending.images, drafts)
        XCTAssertEqual(try fixture.disk.load().pending, pending)

        let recovered = fixture.store()
        XCTAssertEqual(recovered.state.pending, pending)
        XCTAssertEqual(recovered.imageDrafts(for: nil), drafts)
        XCTAssertEqual(try recovered.imageData(for: XCTUnwrap(drafts.first)), bytes)
        let confirmed = await recovered.retryPending()
        XCTAssertEqual(confirmed, pending)
        XCTAssertNil(try fixture.disk.load().pending)
        XCTAssertEqual(recovered.submittedTaskID, "accepted")
        XCTAssertTrue(recovered.imageDrafts(for: nil).isEmpty)
        let posts = capture.posts
        XCTAssertEqual(posts.count, 2)
        XCTAssertEqual(posts.first, posts.last)
        XCTAssertEqual(posts.first?["client_request_id"], .string(pending.clientRequestID))
        XCTAssertEqual(posts.first?["image_ids"], .array(drafts.map { .string($0.imageID) }))
        XCTAssertEqual(capture.uploads.map(\.id), [drafts[0].imageID, drafts[0].imageID])
        XCTAssertEqual(capture.uploads.map(\.data), [bytes, bytes])
    }

    @MainActor func testImageOnlySubmissionUsesTheVisibleDefaultIntentAndClearsMatchingDraft() async throws {
        let capture = XImageRequestCapture()
        let fixture = try successHarness(capture)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setDraft(" \n ", for: nil)
        try store.addImage(data: imageBytes(.systemOrange), for: nil)
        let original = store.imageDrafts(for: nil)
        let confirmed = await store.submit(nodeID: "mac-a", projectID: "accept", prompt: " \n ")
        XCTAssertEqual(confirmed?.content, AppStore.imageOnlyPrompt)
        XCTAssertEqual(confirmed?.images, original)
        XCTAssertEqual(capture.posts.last?["prompt"], .string(AppStore.imageOnlyPrompt))
        XCTAssertEqual(capture.posts.last?["image_ids"], .array(original.map { .string($0.imageID) }))
        XCTAssertEqual(store.draft(for: nil), "")
        XCTAssertTrue(store.imageDrafts(for: nil).isEmpty)
        XCTAssertNil(try fixture.disk.load().pending)
    }

    @MainActor func testChangingOnlyImagesDuringSubmissionKeepsTheNewDraft() async throws {
        let capture = XImageRequestCapture()
        let gate = XImageReplyGate()
        let fixture = try successHarness(capture, gate: gate)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        let sentBytes = try imageBytes(.systemTeal)
        let nextBytes = try imageBytes(.systemPurple)
        store.setDraft("文字相同，图片不同", for: nil)
        try store.addImage(data: sentBytes, for: nil)
        let original = try XCTUnwrap(store.imageDrafts(for: nil).first)
        let submission = Task { await store.submit(nodeID: "mac-a", projectID: "accept", prompt: "文字相同，图片不同") }
        await fulfillment(of: [gate.received], timeout: 5)
        XCTAssertTrue(store.isSending)
        XCTAssertEqual(store.state.pending?.imageIDs, [original.imageID])
        store.removeImage(id: original.id, for: nil)
        try store.addImage(data: nextBytes, for: nil)
        let changed = store.imageDrafts(for: nil)
        XCTAssertEqual(try store.imageData(for: original), sentBytes)
        XCTAssertEqual(store.state.pending?.images, [original])
        gate.complete(Self.accepted)
        let confirmed = await submission.value
        XCTAssertEqual(confirmed?.imageIDs, [original.imageID])
        XCTAssertEqual(store.draft(for: nil), "文字相同，图片不同")
        XCTAssertEqual(store.imageDrafts(for: nil), changed)
        XCTAssertEqual(try store.imageData(for: XCTUnwrap(changed.first)), nextBytes)
        XCTAssertEqual(try fixture.disk.load().photoDrafts["new"], changed)
        XCTAssertNil(store.state.pending)
        XCTAssertEqual(capture.posts.last?["image_ids"], .array([.string(original.imageID)]))
    }

    @MainActor func testFollowupMovesTheWholeEditedImageDraftToTheAcceptedChild() async throws {
        let capture = XImageRequestCapture()
        let gate = XImageReplyGate()
        let fixture = try successHarness(capture, gate: gate)
        defer { fixture.cleanUp() }
        let store = fixture.store()
        store.setDraft("继续看这张图", for: "parent")
        try store.addImage(data: imageBytes(.systemBlue), for: "parent")
        let sent = try XCTUnwrap(store.imageDrafts(for: "parent").first)
        let submission = Task { await store.followup(taskID: "parent", message: "继续看这张图") }
        await fulfillment(of: [gate.received], timeout: 5)
        store.removeImage(id: sent.id, for: "parent")
        let replacement = try imageBytes(.systemGreen)
        try store.addImage(data: replacement, for: "parent")
        let changed = store.imageDrafts(for: "parent")
        gate.complete(Self.accepted)
        let confirmed = await submission.value
        XCTAssertEqual(confirmed?.followsTaskID, "parent")
        XCTAssertEqual(confirmed?.imageIDs, [sent.imageID])
        XCTAssertEqual(store.draft(for: "accepted"), "继续看这张图")
        XCTAssertEqual(store.imageDrafts(for: "accepted"), changed)
        XCTAssertEqual(try store.imageData(for: XCTUnwrap(changed.first)), replacement)
        XCTAssertEqual(store.draft(for: "parent"), "")
        XCTAssertTrue(store.imageDrafts(for: "parent").isEmpty)
        let body = try XCTUnwrap(capture.posts.last)
        XCTAssertEqual(body["message"], .string("继续看这张图"))
        XCTAssertNil(body["node_id"])
        XCTAssertNil(body["project_id"])
        XCTAssertEqual(try fixture.disk.load().photoDrafts["accepted"], changed)
    }

    @MainActor func testCorruptImageCacheIsVerifiedAndReplacedFromAuthenticatedDownload() async throws {
        let bytes = try imageBytes(.systemTeal)
        let image = try Self.metadata(bytes, id: "i-" + String(repeating: "a", count: 32), direction: "output")
        let capture = XImageRequestCapture()
        let fixture = try XImageTestHarness { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/xvis/api/v1/tasks/task/images/\(image.image_id)/content")
            XCTAssertNil(request.url?.query)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer image-test-only")
            capture.recordDownload(image.image_id)
            return .data(200, bytes)
        }
        defer { fixture.cleanUp() }
        let cacheName = try ImageDisk.cachedName(image)
        var corrupt = bytes
        corrupt[corrupt.count - 5] ^= 1
        try fixture.images.write(corrupt, named: cacheName)
        let store = fixture.store()
        let repaired = try await store.loadImage(image, taskID: "task")
        XCTAssertEqual(repaired, bytes)
        XCTAssertEqual(try fixture.images.read(cacheName), bytes)
        let cached = try await store.loadImage(image, taskID: "task")
        XCTAssertEqual(cached, bytes)
        XCTAssertEqual(capture.downloads, [image.image_id])
    }

    @MainActor func testWrongSHAFromDownloadIsRejectedAndNeverCachedAsSuccess() async throws {
        let bytes = try imageBytes(.systemTeal)
        let image = try Self.metadata(bytes, id: "i-" + String(repeating: "b", count: 32), direction: "output")
        var corrupt = bytes
        corrupt[corrupt.count - 5] ^= 1
        let fixture = try XImageTestHarness { _ in .data(200, corrupt) }
        defer { fixture.cleanUp() }
        let store = fixture.store()
        do {
            _ = try await store.loadImage(image, taskID: "task")
            XCTFail("A download whose bytes do not match the declared SHA must fail.")
        } catch AppFailure.protocolMismatch { }
        catch { XCTFail("Expected an image integrity failure.") }
        XCTAssertThrowsError(try fixture.images.read(ImageDisk.cachedName(image)))
    }

    @MainActor func testRetryFailedTaskKeepsOriginalInputImagesAndDoesNotAttachOutputsOrCurrentDraft() async throws {
        let bytes = try imageBytes(.systemBlue)
        let input = try Self.metadata(bytes, id: "i-" + String(repeating: "c", count: 32))
        let output = try Self.metadata(bytes, id: "i-" + String(repeating: "d", count: 32), direction: "output")
        let capture = XImageRequestCapture()
        let fixture = try XImageTestHarness { request in
            if request.httpMethod == "GET", request.url?.lastPathComponent == "content" {
                XCTAssertTrue(request.url?.path.contains(input.image_id) == true)
                capture.recordDownload(input.image_id)
                return .data(200, bytes)
            }
            if request.httpMethod == "PUT" {
                let data = try XCTUnwrap(request.httpBody)
                let id = try XCTUnwrap(request.url?.lastPathComponent)
                capture.recordUpload(id: id, data: data)
                return .data(200, try JSONEncoder().encode(Self.metadata(data, id: id)))
            }
            if request.httpMethod == "POST" {
                capture.recordPost(try Self.body(request))
                return Self.accepted
            }
            return Self.listReply(request)
        }
        defer { fixture.cleanUp() }
        let value: [String: Any] = ["task_id": "failed", "status": "failed", "node_id": "mac-a", "project_id": "accept",
                                  "created_at": "2026-10-08T00:00:00Z", "prompt": "原始图片任务", "can_followup": false,
                                  "images": try JSONSerialization.jsonObject(with: JSONEncoder().encode([input, output]))]
        let task = try JSONDecoder().decode(TaskView.self, from: JSONSerialization.data(withJSONObject: value))
        let store = fixture.store()
        store.setDraft("当前草稿保留", for: nil)
        try store.addImage(data: imageBytes(.systemOrange), for: nil)
        let currentImages = store.imageDrafts(for: nil)
        await store.retryTaskWithImages(task)
        XCTAssertEqual(store.lastConfirmedSubmission?.content, "原始图片任务")
        XCTAssertEqual(store.lastConfirmedSubmission?.imageIDs, [input.image_id])
        XCTAssertEqual(capture.uploads, [XImageUpload(id: input.image_id, data: bytes)])
        XCTAssertEqual(capture.downloads, [input.image_id])
        XCTAssertEqual(capture.posts.last?["image_ids"], .array([.string(input.image_id)]))
        XCTAssertEqual(store.draft(for: nil), "当前草稿保留")
        XCTAssertEqual(store.imageDrafts(for: nil), currentImages)
    }

    func testLegacyPendingSubmissionAndStateDoNotRequireImageFields() throws {
        let data = Data("""
        {"baseURL":"https://example.com/xvis/","pending":{"clientRequestID":"legacy","nodeID":"mac-a","projectID":"accept","content":"原文","createdAt":0}}
        """.utf8)
        let restored = try JSONDecoder().decode(SavedState.self, from: data)
        let pending = try XCTUnwrap(restored.pending)
        XCTAssertTrue(restored.photoDrafts.isEmpty)
        XCTAssertNil(pending.images)
        XCTAssertTrue(pending.imageIDs.isEmpty)
        XCTAssertEqual(Set(pending.body.keys), ["client_request_id", "node_id", "project_id", "prompt"])
    }

    @MainActor private func imageBytes(_ color: UIColor) throws -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let image = UIGraphicsImageRenderer(size: CGSize(width: 12, height: 8), format: format).image { context in
            color.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 12, height: 8))
        }
        return try XCTUnwrap(image.jpegData(compressionQuality: 0.85))
    }

    private static func metadata(_ data: Data, id: String, direction: String = "input") throws -> ImageView {
        ImageView(image_id: id, mime_type: try ImageDisk.contentType(data), size_bytes: data.count,
                  sha256: ImageDisk.digest(data), width: 12, height: 8, direction: direction)
    }

    private static func body(_ request: URLRequest) throws -> [String: JSONValue] {
        try JSONDecoder().decode([String: JSONValue].self, from: XCTUnwrap(request.httpBody))
    }

    private static var accepted: XImageStubReply {
        .data(201, Data("{\"task_id\":\"accepted\",\"status\":\"queued\"}".utf8))
    }

    private static func listReply(_ request: URLRequest) -> XImageStubReply {
        switch request.url?.lastPathComponent {
        case "nodes": return .data(200, Data("{\"nodes\":[]}".utf8))
        case "projects": return .data(200, Data("{\"projects\":[]}".utf8))
        case "tasks": return .data(200, Data("{\"tasks\":[],\"next_cursor\":null}".utf8))
        default: return .failure(URLError(.unsupportedURL))
        }
    }

    private func successHarness(_ capture: XImageRequestCapture, gate: XImageReplyGate? = nil) throws -> XImageTestHarness {
        try XImageTestHarness { request in
            if request.httpMethod == "PUT" {
                let data = try XCTUnwrap(request.httpBody)
                let id = try XCTUnwrap(request.url?.lastPathComponent)
                capture.recordUpload(id: id, data: data)
                return .data(200, try JSONEncoder().encode(Self.metadata(data, id: id)))
            }
            if request.httpMethod == "POST" {
                capture.recordPost(try Self.body(request))
                return gate.map(XImageStubReply.deferred) ?? Self.accepted
            }
            return Self.listReply(request)
        }
    }
}

private struct XImageUpload: Equatable { let id: String; let data: Data }

private final class XImageRequestCapture {
    private let lock = NSLock()
    private var savedPosts: [[String: JSONValue]] = []
    private var savedUploads: [XImageUpload] = []
    private var savedDownloads: [String] = []
    private var lostResponse = false
    var posts: [[String: JSONValue]] { lock.lock(); defer { lock.unlock() }; return savedPosts }
    var uploads: [XImageUpload] { lock.lock(); defer { lock.unlock() }; return savedUploads }
    var downloads: [String] { lock.lock(); defer { lock.unlock() }; return savedDownloads }
    func recordPost(_ body: [String: JSONValue]) { lock.lock(); defer { lock.unlock() }; savedPosts.append(body) }
    func recordUpload(id: String, data: Data) { lock.lock(); defer { lock.unlock() }; savedUploads.append(XImageUpload(id: id, data: data)) }
    func recordDownload(_ id: String) { lock.lock(); defer { lock.unlock() }; savedDownloads.append(id) }
    func takeFirstResponseLoss() -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !lostResponse else { return false }
        lostResponse = true
        return true
    }
}

private final class XImageTestCredentials: CredentialStore {
    private var value: ConnectionCredential? = ConnectionCredential(endpoint: "https://example.com/xvis/", token: "image-test-only")
    func read() throws -> ConnectionCredential? { value }
    func save(_ credential: ConnectionCredential) throws { value = credential }
    func clear() throws { value = nil }
}

private final class XImageTestHarness {
    let directory: URL
    let disk: StateDisk
    let images: ImageDisk
    let session: URLSession
    private let route: String

    init(handler: @escaping (URLRequest) throws -> XImageStubReply) throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("XVIS-ImageTests-\(UUID().uuidString)")
        disk = StateDisk(directory: directory)
        images = ImageDisk(directory: directory)
        var state = SavedState()
        state.baseURL = "https://example.com/xvis/"
        try disk.save(state)
        route = XImageStubProtocol.register(handler)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [XImageStubProtocol.self]
        config.httpAdditionalHeaders = [XImageStubProtocol.routeHeader: route]
        session = URLSession(configuration: config)
    }

    @MainActor func store() -> AppStore {
        AppStore(disk: disk, credentials: XImageTestCredentials(),
                 clientFactory: { [session] in APIClient(baseURL: $0, token: $1, session: session) })
    }

    func cleanUp() {
        session.invalidateAndCancel()
        XImageStubProtocol.unregister(route)
        try? FileManager.default.removeItem(at: directory)
    }
}

private enum XImageStubReply {
    case data(Int, Data)
    case failure(Error)
    case deferred(XImageReplyGate)
}

private final class XImageReplyGate {
    let received = XCTestExpectation(description: "The image submission reached the server before its acknowledgement.")
    private let lock = NSLock()
    private var connection: XImageStubProtocol?
    func attach(_ connection: XImageStubProtocol) {
        lock.lock(); self.connection = connection; lock.unlock()
        received.fulfill()
    }
    func complete(_ reply: XImageStubReply) {
        lock.lock(); let held = connection; connection = nil; lock.unlock()
        held?.deliver(reply)
    }
}

private final class XImageStubProtocol: URLProtocol {
    static let routeHeader = "X-XVIS-Image-Test-Route"
    private static let registryLock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> XImageStubReply] = [:]
    private let loadingLock = NSLock()
    private var stopped = false

    static func register(_ handler: @escaping (URLRequest) throws -> XImageStubReply) -> String {
        let key = UUID().uuidString
        registryLock.lock(); defer { registryLock.unlock() }
        handlers[key] = handler
        return key
    }
    static func unregister(_ key: String) {
        registryLock.lock(); defer { registryLock.unlock() }
        handlers.removeValue(forKey: key)
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var normalized = request
        if normalized.httpBody == nil, let stream = normalized.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: 4096)
                if count <= 0 { break }
                bytes.append(contentsOf: buffer.prefix(count))
            }
            normalized.httpBody = bytes
        }
        let key = normalized.value(forHTTPHeaderField: Self.routeHeader) ?? ""
        Self.registryLock.lock(); let handler = Self.handlers[key]; Self.registryLock.unlock()
        do {
            guard let handler else { throw URLError(.unsupportedURL) }
            deliver(try handler(normalized))
        } catch { deliver(.failure(error)) }
    }
    func deliver(_ reply: XImageStubReply) {
        loadingLock.lock(); let cancelled = stopped; loadingLock.unlock()
        guard !cancelled else { return }
        switch reply {
        case .data(let status, let bytes):
            guard let url = request.url,
                  let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil,
                                                 headerFields: ["Content-Type": "application/json"]) else { return }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: bytes)
            client?.urlProtocolDidFinishLoading(self)
        case .failure(let error): client?.urlProtocol(self, didFailWithError: error)
        case .deferred(let gate): gate.attach(self)
        }
    }
    override func stopLoading() { loadingLock.lock(); stopped = true; loadingLock.unlock() }
}

final class AssistantProfileTests: XCTestCase {
    @MainActor func testProfileSaveUsesRevisionAndPersistsTheServerAcknowledgement() async throws {
        let initial = AssistantProfile()
        let fixture = try XImageTestHarness { request in
            XCTAssertEqual(request.url?.path, "/xvis/api/v1/assistant-profile")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer image-test-only")
            if request.httpMethod == "PUT" {
                var saved = try JSONDecoder().decode(AssistantProfile.self, from: XCTUnwrap(request.httpBody))
                XCTAssertEqual(saved.revision, 0)
                XCTAssertEqual(saved.memories.first?.text, "沟通时先给出下一步。")
                saved.revision += 1
                return .data(200, try JSONEncoder().encode(saved))
            }
            return .data(200, try JSONEncoder().encode(initial))
        }
        defer { fixture.cleanUp() }
        let store = fixture.store()
        var draft = try await store.fetchAssistantProfile()
        draft.name = "知行"; draft.persona_enabled = true; draft.persona = "清晰、沉静，先说明结论。"
        draft.memory_enabled = true; draft.memories = [AssistantMemory(text: "沟通时先给出下一步。")]
        let saved = try await store.saveAssistantProfile(draft)
        XCTAssertEqual(saved.revision, 1)
        XCTAssertEqual(store.state.assistantProfile, saved)
        XCTAssertEqual(try fixture.disk.load().assistantProfile, saved)
        XCTAssertEqual(fixture.store().state.assistantProfile, saved)
    }

    @MainActor func testProfileConflictKeepsTheEditedDraftAndTheConfirmedCache() async throws {
        let initial = AssistantProfile()
        let fixture = try XImageTestHarness { request in
            if request.httpMethod == "PUT" {
                return .data(409, Data("{\"error\":{\"code\":\"profile_conflict\",\"message\":\"资料已更新\"}}".utf8))
            }
            return .data(200, try JSONEncoder().encode(initial))
        }
        defer { fixture.cleanUp() }
        let store = fixture.store()
        var draft = try await store.fetchAssistantProfile()
        draft.persona = "我正在编辑的说明"; draft.persona_enabled = true
        do { _ = try await store.saveAssistantProfile(draft); XCTFail("A stale revision must not overwrite the profile.") }
        catch AppFailure.server(_, let code, _) { XCTAssertEqual(code, "profile_conflict") }
        catch { XCTFail("Expected the explicit profile revision conflict.") }
        XCTAssertEqual(draft.persona, "我正在编辑的说明")
        XCTAssertEqual(store.state.assistantProfile, initial)
        XCTAssertEqual(try fixture.disk.load().assistantProfile, initial)
    }

    @MainActor func testUnacknowledgedProfileRevisionNeverReplacesTheSavedCache() async throws {
        let initial = AssistantProfile()
        let fixture = try XImageTestHarness { request in
            return .data(200, try JSONEncoder().encode(initial))
        }
        defer { fixture.cleanUp() }
        let store = fixture.store()
        var draft = try await store.fetchAssistantProfile()
        draft.name = "待确认名称"
        do { _ = try await store.saveAssistantProfile(draft); XCTFail("Saving must require a new server revision.") }
        catch AppFailure.protocolMismatch { }
        catch { XCTFail("Expected a protocol mismatch.") }
        XCTAssertEqual(try fixture.disk.load().assistantProfile, initial)
    }

    func testProfileValidationCountsUTF8AndDisabledMemoryStillRoundTrips() throws {
        var profile = AssistantProfile()
        profile.memories = [AssistantMemory(text: "这是手动保存的背景资料", enabled: false)]
        XCTAssertNil(profile.validationMessage)
        let decoded = try JSONDecoder().decode(AssistantProfile.self, from: JSONEncoder().encode(profile))
        XCTAssertFalse(decoded.memory_enabled)
        XCTAssertEqual(decoded.memories, profile.memories)
        profile.persona = String(repeating: "中文", count: 700)
        XCTAssertNotNil(profile.validationMessage)
        profile.persona = ""; profile.memories[0].text = " "
        XCTAssertNotNil(profile.validationMessage)
        let legacy = try JSONDecoder().decode(SavedState.self, from: Data("{}".utf8))
        XCTAssertNil(legacy.assistantProfile)
    }
}
