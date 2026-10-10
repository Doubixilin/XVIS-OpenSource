import Foundation

struct NodeView: Codable, Identifiable {
    var node_id: String
    var name: String
    var online: Bool
    var last_seen: String?
    var busy: Bool
    var id: String { node_id }
}

struct ProjectView: Codable, Identifiable {
    static let defaultWorkspaceID = "p-xvis-test"

    var project_id: String
    var name: String
    var node_id: String
    var available: Bool
    var execution_mode: String? = nil
    var id: String { project_id }
    var scopedID: String { node_id + "|" + project_id }

    static func defaultWorkspace(in projects: [ProjectView]) -> ProjectView? {
        let candidates = projects.filter { $0.project_id == defaultWorkspaceID }
        if let primary = candidates.first(where: { $0.node_id == "mac-a" }) { return primary.available ? primary : nil }
        return candidates.count == 1 && candidates[0].available ? candidates[0] : nil
    }
}

struct TaskView: Codable, Identifiable {
    var task_id: String
    var status: String
    var node_id: String
    var project_id: String
    var created_at: String
    var title: String?
    var prompt: String?
    var started_at: String?
    var finished_at: String?
    var result: ResultView?
    var error: TaskErrorView?
    var can_followup: Bool
    var followup_task_id: String?
    var is_followup_of: String?
    var images: [ImageView]? = nil
    var assistant_name: String? = nil
    var output_files: [OutputFile]? = nil
    var file_transfer: FileTransfer? = nil
    var project_context_revision: Int? = nil
    var project_context: ProjectContext? = nil
    var waiting_reason: String? = nil
    var execution_mode: String? = nil
    var interactions: [TaskInteraction]? = nil
    var control: TaskControl? = nil
    var id: String { task_id }
    var terminal: Bool { ["done", "failed", "cancelled", "timed_out", "interrupted"].contains(status) }
    var statusLabel: String {
        switch status {
        case "queued": return "等待执行"
        case "assigned": return "正在准备"
        case "running": return (interactions ?? []).contains { $0.status == "pending" } ? "待我处理" : "正在执行"
        case "cancel_requested": return "已请求取消，等待确认停止"
        case "done": return "已完成"
        case "failed": return "执行失败"
        case "cancelled": return "已取消"
        case "timed_out": return "执行超时"
        case "interrupted": return "执行中断，请在执行设备上检查"
        default: return "正在核对状态"
        }
    }
}

struct ResultView: Codable {
    var final_text: String
    var truncated: Bool
    var full_text_ref: String?
}

struct TaskErrorView: Codable { var code: String; var message: String }
struct NodesResponse: Decodable { var nodes: [NodeView] }
struct ProjectsResponse: Decodable { var projects: [ProjectView] }
struct TasksResponse: Decodable { var tasks: [TaskView]; var next_cursor: String? }
struct SubmissionResponse: Decodable { var task_id: String; var status: String; var node_online: Bool? }
struct CancelResponse: Decodable { var task_id: String; var status: String; var cancel: String }

enum JSONValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .array(try c.decode([JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    var text: String? { if case .string(let s) = self { return s }; return nil }
    var flag: Bool? { if case .bool(let b) = self { return b }; return nil }
}

struct EventView: Codable, Identifiable {
    var id: Int
    var task_id: String
    var type: String
    var payload: [String: JSONValue]
    var created_at: String
}
struct EventsResponse: Decodable { var events: [EventView]; var next_after: Int; var has_more: Bool }

struct StepEntry: Codable, Identifiable {
    var id: Int
    var title: String
    var text: String
    var eventType: String? = nil
    var createdAt: String? = nil
    var tool: String? = nil
    var status: String? = nil
    var phase: String? = nil
}

struct TaskCache: Codable {
    static let displayByteLimit = 256 * 1024
    var snapshot: TaskView?
    var after = 0
    var steps: [StepEntry] = []
    var finalText = ""
    var finalStarted = false
    var finalComplete = false
    var finalTruncated = false

    mutating func applySnapshot(_ task: TaskView) {
        snapshot = task
        if !finalStarted, let result = task.result {
            finalText = Self.prefix(result.final_text, bytes: Self.displayByteLimit)
            finalTruncated = result.truncated || result.final_text.utf8.count > Self.displayByteLimit
        }
    }

    // Return a new value so a failed page never advances the persisted cursor.
    func consuming(_ page: EventsResponse, taskID: String) throws -> TaskCache {
        var next = self
        var lastID = after
        for event in page.events {
            guard event.task_id == taskID, event.id > lastID else { throw AppFailure.protocolMismatch }
            lastID = event.id
            try next.consume(event)
        }
        guard page.next_after == lastID,
              !(page.has_more && page.next_after <= after) else { throw AppFailure.protocolMismatch }
        next.after = page.next_after
        return next
    }

    private mutating func consume(_ event: EventView) throws {
        let p = event.payload
        let text = p["text"]?.text ?? ""
        if event.type == "answer_reset" || event.type == "answer_delta" {
            if finalStarted { return }
            if event.type == "answer_reset" { finalText = ""; finalTruncated = false }
            else {
                guard p["text"]?.text != nil else { throw AppFailure.protocolMismatch }
                let room = max(0, Self.displayByteLimit - finalText.utf8.count)
                finalText += Self.prefix(text, bytes: room)
                finalTruncated = finalTruncated || text.utf8.count > room
            }
            return
        }
        if event.type == "control_state", let value = p["control"],
           let bytes = try? JSONEncoder().encode(value), let control = try? JSONDecoder().decode(TaskControl.self, from: bytes) {
            guard control.isValid else { throw AppFailure.protocolMismatch }
            var updated = control; updated.commands = snapshot?.control?.commands
            snapshot?.control = updated
            return
        }
        if event.type == "final" || event.type == "final_chunk" {
            guard p["text"]?.text != nil else { throw AppFailure.protocolMismatch }
            if !finalStarted { finalText = ""; finalTruncated = false; finalStarted = true }
            if event.type == "final" { finalText = ""; finalTruncated = false }
            let room = max(0, Self.displayByteLimit - finalText.utf8.count)
            finalText += Self.prefix(text, bytes: room)
            finalTruncated = finalTruncated || text.utf8.count > room || p["truncated"]?.flag == true
            finalComplete = event.type == "final" || p["seq_end"]?.flag == true
            return
        }
        var title: String?
        var detail = ""
        switch event.type {
        case "text": title = "回答片段"; detail = text
        case "tool_call": title = "正在使用工具"; detail = toolLabel(p["tool"]?.text)
        case "tool_result":
            title = "工具执行结果"
            detail = p["summary"]?.text ?? p["result"]?.text ?? text
            if detail.isEmpty { detail = p["status"]?.text == "error" ? "工具受到限制或执行失败" : "工具调用已返回" }
        case "error": title = "执行提示"; detail = p["message"]?.text ?? "执行发生错误，请在执行设备上查看记录。"
        case "status":
            switch p["phase"]?.text {
            case "turn_start": title = "任务已开始"
            case "step_start": title = "开始下一步"
            case "step_end": title = "本步骤结束"
            case "turn_end": title = "本轮执行结束"
            default: break
            }
        default: break
        }
        if let title {
            steps.append(StepEntry(id: event.id, title: title, text: Self.prefix(detail, bytes: 8192),
                                   eventType: event.type, createdAt: event.created_at,
                                   tool: p["tool"]?.text, status: p["status"]?.text, phase: p["phase"]?.text))
            if steps.count > 300 { steps.removeFirst(steps.count - 300) }
        }
    }

    private func toolLabel(_ tool: String?) -> String {
        switch tool {
        case "bash": return "运行项目命令"
        case "write": return "写入项目文件"
        case "read": return "读取文件"
        case "edit": return "修改项目文件"
        default: return "处理项目内容"
        }
    }

    static func prefix(_ string: String, bytes: Int) -> String {
        guard bytes > 0 else { return "" }
        let utf8 = string.utf8
        if utf8.count <= bytes { return string }
        var slice = Array(utf8.prefix(bytes))
        while String(bytes: slice, encoding: .utf8) == nil { slice.removeLast() }
        return String(bytes: slice, encoding: .utf8) ?? ""
    }
}

struct PendingSubmission: Codable, Equatable {
    var clientRequestID: String
    var nodeID: String?
    var projectID: String?
    var followsTaskID: String?
    var content: String
    var createdAt: Date
    var images: [PhotoDraft]? = nil
    var imageIDs: [String] { (images ?? []).map(\.imageID) }
    static func newTask(nodeID: String, projectID: String, prompt: String, images: [PhotoDraft] = []) -> PendingSubmission {
        PendingSubmission(clientRequestID: UUID().uuidString, nodeID: nodeID, projectID: projectID,
                          content: prompt, createdAt: Date(), images: images.isEmpty ? nil : images)
    }
    static func followup(taskID: String, message: String, images: [PhotoDraft] = []) -> PendingSubmission {
        PendingSubmission(clientRequestID: UUID().uuidString, followsTaskID: taskID, content: message, createdAt: Date(), images: images.isEmpty ? nil : images)
    }
    var body: [String: String] {
        if followsTaskID != nil { return ["client_request_id": clientRequestID, "message": content] }
        return ["client_request_id": clientRequestID, "node_id": nodeID ?? "", "project_id": projectID ?? "", "prompt": content]
    }
}
