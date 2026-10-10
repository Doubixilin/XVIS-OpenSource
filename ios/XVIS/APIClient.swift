import Foundation

private struct APIErrorBody: Decodable { var error: TaskErrorView }

enum AppFailure: LocalizedError {
    case invalidAddress, noConnection, connectionMismatch, protocolMismatch, tooLong, pendingExists, localSave, credentialSave
    case server(Int, String, String)
    case network(String)
    var errorDescription: String? {
        switch self {
        case .invalidAddress: return "请输入完整的 HTTPS 服务器地址，例如 https://example.com/xvis/。"
        case .noConnection: return "请先在连接设置中填写服务器地址和访问凭证。"
        case .connectionMismatch: return "保存的服务器地址与访问凭证不匹配，请重新配置连接。"
        case .protocolMismatch: return "服务器返回的记录不完整，已保留当前位置。稍后可重新刷新。"
        case .tooLong: return "内容较长，请缩短后再提交。"
        case .pendingExists: return "上次提交尚未确认，请先原样重试或明确放弃该请求。"
        case .localSave: return "手机未能保存请求或记录，本次操作未继续。请检查可用存储后重试。"
        case .credentialSave: return "无法安全保存访问凭证，请解锁手机后重试。"
        case .server(let status, let code, let message):
            switch status {
            case 401: return "访问凭证无效或已更换，请在连接设置中重新填写。"
            case 403: return "该凭证无法控制任务，请使用手机访问凭证。"
            case 404 where code == "file_not_found": return "此文件没有关联到本轮任务，请刷新文件状态。"
            case 404: return "任务或服务地址不存在，请检查连接地址。"
            case 409 where code == "request_id_conflict": return "这次请求与已保存的内容不一致。为避免重复执行，未创建新的请求，请检查服务器记录。"
            case 409 where code == "profile_conflict": return "助理资料已在其他位置更新。你的编辑稿已保留，请重新载入后合并修改。"
            case 409 where code == "project_context_conflict": return "项目背景已在其他位置更新。你的编辑稿已保留，请重新载入最新资料后合并修改。"
            case 409 where code == "result_revision_conflict": return "这轮结果已更新，已保留未查看状态，请重新打开结果。"
            case 409 where code == "file_not_ready": return "成果文件尚未传回，可刷新状态或请求补传；任务不会重跑。"
            case 409 where code == "file_not_retryable": return "当前没有可补传的成果文件，请刷新文件状态。"
            case 409 where code.hasPrefix("interaction_"): return message
            case 409 where code.hasPrefix("control_"): return message
            case 409: return "该任务当前无法继续，请刷新任务状态。"
            case 429: return "提交过于频繁，请稍后原样重试。"
            case 503 where code == "file_unavailable": return message.isEmpty ? "成果文件暂时无法读取，可请求补传后重新下载。" : message
            case 503: return "等待执行的任务较多，请稍后原样重试。"
            default: return message.isEmpty ? "服务器暂时无法完成操作，请稍后重试。" : message
            }
        case .network(let text): return text
        }
    }
}

struct APIClient {
    private static let transport: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        return URLSession(configuration: configuration, delegate: NoRedirectDelegate(), delegateQueue: nil)
    }()
    let baseURL: URL
    let token: String
    let session: URLSession
    init(baseURL: URL, token: String, session: URLSession? = nil) {
        self.baseURL = baseURL; self.token = token; self.session = session ?? Self.transport
    }
    static func validatedURL(_ input: String) throws -> URL {
        guard var c = URLComponents(string: input.trimmingCharacters(in: .whitespacesAndNewlines)),
              c.scheme?.lowercased() == "https", let host = c.host, !host.isEmpty,
              c.user == nil, c.password == nil, c.query == nil, c.fragment == nil else { throw AppFailure.invalidAddress }
        if !c.path.hasSuffix("/") { c.path += "/" }
        guard let url = c.url else { throw AppFailure.invalidAddress }
        return url
    }
    func url(path: String, query: [URLQueryItem] = []) throws -> URL {
        guard !path.hasPrefix("/"), !path.contains("..") else { throw AppFailure.invalidAddress }
        guard var c = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false) else { throw AppFailure.invalidAddress }
        if !query.isEmpty { c.queryItems = query }
        guard let url = c.url else { throw AppFailure.invalidAddress }
        return url
    }
    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try await request(path, method: "GET", query: query)
    }
    func post<T: Decodable>(_ path: String, body: [String: String], imageIDs: [String] = []) async throws -> T {
        try await request(path, method: "POST", body: body, imageIDs: imageIDs)
    }
    func uploadImage(id: String, data: Data) async throws -> ImageView {
        guard id.range(of: "^i-[0-9a-f]{32}$", options: .regularExpression) != nil else { throw AppFailure.protocolMismatch }
        var r = URLRequest(url: try url(path: "api/v1/images/\(id)"))
        r.httpMethod = "PUT"; r.timeoutInterval = 40; r.httpBody = data
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue(try ImageDisk.contentType(data), forHTTPHeaderField: "Content-Type")
        let bytes = try await perform(r)
        guard let image = try? JSONDecoder().decode(ImageView.self, from: bytes), image.image_id == id else { throw AppFailure.protocolMismatch }
        try ImageDisk.verify(data, against: image)
        return image
    }
    func imageData(_ image: ImageView, taskID: String?) async throws -> Data {
        let path = taskID.map { "api/v1/tasks/\($0)/images/\(image.image_id)/content" } ?? "api/v1/images/\(image.image_id)/content"
        var r = URLRequest(url: try url(path: path))
        r.timeoutInterval = 40
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("image/jpeg, image/png", forHTTPHeaderField: "Accept")
        return try await perform(r)
    }
    func respondToInteraction(_ item: TaskInteraction, submission: InteractionSubmission) async throws -> TaskInteraction {
        guard item.isValid else { throw AppFailure.protocolMismatch }
        var r = URLRequest(url: try url(path: "api/v1/tasks/\(item.task_id)/interactions/\(item.id)/respond"))
        r.httpMethod = "POST"; r.timeoutInterval = 20
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONEncoder().encode(submission)
        let bytes = try await perform(r)
        guard let response = try? JSONDecoder().decode(TaskInteraction.self, from: bytes) else { throw AppFailure.protocolMismatch }
        return response
    }
    func submitControl(taskID: String, command: ControlCommand) async throws -> ControlReceipt {
        guard XNotificationID.safeTask(taskID) else { throw AppFailure.protocolMismatch }
        var r = URLRequest(url: try url(path: "api/v1/tasks/\(taskID)/controls"))
        r.httpMethod = "POST"; r.timeoutInterval = 20
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONEncoder().encode(command)
        return try JSONDecoder().decode(ControlReceipt.self, from: await perform(r))
    }
    func streamEvents(taskID: String, after: Int, receive: @MainActor (EventsResponse) throws -> Void) async throws {
        guard XNotificationID.safeTask(taskID), after >= 0 else { throw AppFailure.protocolMismatch }
        var r = URLRequest(url: try url(path: "api/v1/tasks/\(taskID)/stream", query: [.init(name: "after", value: String(after))]))
        r.timeoutInterval = 35
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/x-ndjson", forHTTPHeaderField: "Accept")
        let (bytes, response) = try await session.bytes(for: r)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else { throw AppFailure.protocolMismatch }
        for try await line in bytes.lines {
            try Task.checkCancellation()
            if line.isEmpty { continue }
            guard line.utf8.count <= 7 * 1024 * 1024 else { throw AppFailure.protocolMismatch }
            let page = try JSONDecoder().decode(EventsResponse.self, from: Data(line.utf8))
            try await receive(page)
        }
    }
    func saveProfile(_ profile: AssistantProfile) async throws -> AssistantProfile {
        var r = URLRequest(url: try url(path: "api/v1/assistant-profile"))
        r.httpMethod = "PUT"; r.timeoutInterval = 20
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONEncoder().encode(profile)
        let data = try await perform(r)
        guard let saved = try? JSONDecoder().decode(AssistantProfile.self, from: data) else { throw AppFailure.protocolMismatch }
        return saved
    }
    func confirmInboxRead(id: String, revision: Int) async throws -> InboxItemResponse {
        guard XNotificationID.matches(id, prefix: "r"), revision > 0 else { throw AppFailure.protocolMismatch }
        return try await notificationRequest("api/v1/inbox/\(id)/read", method: "PUT", body: ["revision": revision])
    }
    func saveNotificationDevice(id: String, token: String, environment: String, enabled: Bool) async throws -> NotificationDeviceResponse {
        guard XNotificationID.matches(id, prefix: "d"), XNotificationID.validToken(token),
              ["sandbox", "production"].contains(environment) else { throw AppFailure.protocolMismatch }
        return try await notificationRequest("api/v1/notification-devices/\(id)", method: "PUT",
                                             body: ["token": token, "environment": environment, "enabled": enabled])
    }
    func deleteNotificationDevice(id: String) async throws -> NotificationDeviceResponse {
        guard XNotificationID.matches(id, prefix: "d") else { throw AppFailure.protocolMismatch }
        return try await notificationRequest("api/v1/notification-devices/\(id)", method: "DELETE")
    }
    func fileDownload(_ file: OutputFile, taskID: String) async throws -> URL {
        guard file.isValid, file.status == "ready", XNotificationID.safeTask(taskID) else { throw AppFailure.protocolMismatch }
        var r = URLRequest(url: try url(path: "api/v1/tasks/\(taskID)/files/\(file.file_id)/content"))
        r.timeoutInterval = 60
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue(file.mime_type, forHTTPHeaderField: "Accept")
        let downloaded: URL
        let response: URLResponse
        do { (downloaded, response) = try await session.download(for: r) }
        catch let error as URLError {
            if error.code == .cancelled { throw CancellationError() }
            throw AppFailure.network("文件未能下载，请检查连接后重新下载；任务不会重新执行。")
        }
        guard let http = response as? HTTPURLResponse else { try? FileManager.default.removeItem(at: downloaded); throw AppFailure.protocolMismatch }
        guard (200..<300).contains(http.statusCode) else {
            let bytes = try? Data(contentsOf: downloaded)
            try? FileManager.default.removeItem(at: downloaded)
            let error = bytes.flatMap { try? JSONDecoder().decode(APIErrorBody.self, from: $0) }
            throw AppFailure.server(http.statusCode, error?.error.code ?? "", error?.error.message ?? "")
        }
        return downloaded
    }
    static func projectPath(nodeID: String, projectID: String) throws -> String {
        guard XNotificationID.safeTask(nodeID), XNotificationID.safeTask(projectID) else { throw AppFailure.protocolMismatch }
        return "api/v1/nodes/\(nodeID)/projects/\(projectID)/context"
    }
    func saveProjectContext(_ value: ProjectContext, nodeID: String, projectID: String) async throws -> ProjectContext {
        var r = URLRequest(url: try url(path: Self.projectPath(nodeID: nodeID, projectID: projectID)))
        r.httpMethod = "PUT"; r.timeoutInterval = 20
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder(); encoder.outputFormatting = [.withoutEscapingSlashes]
        r.httpBody = try encoder.encode(value.editable)
        let bytes = try await perform(r)
        guard let saved = try? JSONDecoder().decode(ProjectContext.self, from: bytes) else { throw AppFailure.protocolMismatch }
        return saved
    }
    func createProjectSummary(nodeID: String, projectID: String, requestID: String, sourceIDs: [String]) async throws -> SubmissionResponse {
        let path = try Self.projectPath(nodeID: nodeID, projectID: projectID) + "/summary"
        return try await notificationRequest(path, method: "POST", body: ["client_request_id": requestID, "source_task_ids": sourceIDs])
    }
    private func notificationRequest<T: Decodable>(_ path: String, method: String, body: [String: Any]? = nil) async throws -> T {
        var r = URLRequest(url: try url(path: path))
        r.httpMethod = method; r.timeoutInterval = 20
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let bytes = try await perform(r)
        guard let decoded = try? JSONDecoder().decode(T.self, from: bytes) else { throw AppFailure.protocolMismatch }
        return decoded
    }
    private func request<T: Decodable>(_ path: String, method: String, query: [URLQueryItem] = [], body: [String: String]? = nil, imageIDs: [String] = []) async throws -> T {
        var r = URLRequest(url: try url(path: path, query: query))
        r.httpMethod = method; r.timeoutInterval = 20
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            var payload: [String: Any] = body
            if !imageIDs.isEmpty { payload["image_ids"] = imageIDs }
            r.httpBody = try JSONSerialization.data(withJSONObject: payload)
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data = try await perform(r)
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw AppFailure.protocolMismatch }
    }
    private func perform(_ r: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: r) }
        catch let e as URLError {
            if e.code == .cancelled { throw CancellationError() }
            switch e.code {
            case .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot,
                 .serverCertificateNotYetValid, .secureConnectionFailed:
                throw AppFailure.network("无法验证服务器的安全连接，请检查服务器证书和手机时间。")
            case .timedOut: throw AppFailure.network("连接超时，请稍后重试。尚未确认的提交可使用原样重试找回任务。")
            case .notConnectedToInternet, .networkConnectionLost: throw AppFailure.network("网络暂时不可用，请恢复连接后重试。尚未确认的提交可原样重试。")
            default: throw AppFailure.network("暂时无法连接服务器，请检查网络与连接地址。")
            }
        }
        guard let http = response as? HTTPURLResponse else { throw AppFailure.protocolMismatch }
        guard (200..<300).contains(http.statusCode) else {
            let error = try? JSONDecoder().decode(APIErrorBody.self, from: data)
            throw AppFailure.server(http.statusCode, error?.error.code ?? "", error?.error.message ?? "")
        }
        return data
    }
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // Keep credentials at the explicitly configured endpoint.
        completionHandler(nil)
    }
}
