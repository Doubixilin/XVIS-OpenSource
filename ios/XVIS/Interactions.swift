import Foundation

struct ControlItem: Codable, Identifiable {
    var item_id: String
    var request_id: String
    var text: String
    var delivery: String
    var id: String { item_id }
}
struct ControlCommand: Codable, Identifiable {
    var command_id = UUID().uuidString
    var epoch: String
    var action: String
    var text: String? = nil
    var item_id: String? = nil
    var revision: Int? = nil
    var id: String { command_id }
}
struct ControlReceipt: Codable, Identifiable {
    var command_id: String
    var task_id: String
    var epoch: String
    var action: String
    var text: String?
    var status: String
    var detail: String
    var consumed: Bool
    var id: String { command_id }
    var label: String {
        if consumed { return "已用于执行" }
        switch status {
        case "pending": return "等待设备接收"
        case "accepted": return action == "remove" ? "已移除" : "引擎已接收"
        case "rejected": return "未执行，请刷新队列"
        default: return "接收未确认，请查看记录"
        }
    }
}
struct TaskControl: Codable {
    var epoch: String
    var revision: Int
    var items: [ControlItem]
    var commands: [ControlReceipt]?
    var isValid: Bool {
        UUID(uuidString: epoch) != nil && revision >= 0 && items.count <= 20
        && Set(items.map(\.id)).count == items.count
        && items.allSatisfy { UUID(uuidString: $0.id) != nil && UUID(uuidString: $0.request_id) != nil && ["queue", "steer"].contains($0.delivery) }
        && items.reduce(0, { $0 + $1.text.utf8.count }) <= 32768
    }
}

struct InteractionQuestion: Codable, Identifiable {
    struct Option: Codable { var label: String; var description: String? }
    var id: String
    var question: String
    var header: String?
    var options: [Option]?
    var multiSelect: Bool?
}

struct InteractionRequest: Codable {
    var questions: [InteractionQuestion]?
    var tool_name: String?
    var reason: String?
    var arguments: JSONValue?
}

struct TaskInteraction: Codable, Identifiable {
    var interaction_id: String
    var task_id: String
    var revision: Int
    var kind: String
    var epoch: String
    var session_id: String
    var call_id: String
    var expires_at: String
    var request: InteractionRequest
    var status: String
    var outcome: String?
    var response_id: String?
    var response: JSONValue?
    var id: String { interaction_id }
    var isValid: Bool {
        XNotificationID.matches(id, prefix: "q") && XNotificationID.safeTask(task_id) && revision == 1
        && ["question", "approval"].contains(kind)
        && ["pending", "submitted", "confirmed", "cancelled", "expired", "interrupted"].contains(status)
        && expiry != nil && !epoch.isEmpty && !session_id.isEmpty && !call_id.isEmpty
        && (kind == "question" ? !(request.questions ?? []).isEmpty : request.arguments != nil && request.tool_name != nil)
    }
    var expiry: Date? {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return parser.date(from: expires_at) ?? ISO8601DateFormatter().date(from: expires_at)
    }
    var canRespond: Bool { isValid && status == "pending" && (expiry ?? .distantPast) > Date() }
    var label: String {
        switch status {
        case "pending": return canRespond ? (kind == "approval" ? "等待你的批准" : "等待你的答复") : "已过期"
        case "submitted": return "已发送，等待设备确认"
        case "confirmed":
            switch outcome {
            case "allowed-once": return "设备已确认本次批准"
            case "rejected": return "设备已确认拒绝"
            default: return "设备已收到答复"
            }
        case "expired": return "已过期，不能再答复"
        case "cancelled": return "已取消"
        default: return "执行中断，此请求已失效"
        }
    }
    var argumentsText: String {
        guard let arguments = request.arguments else { return "" }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return (try? encoder.encode(arguments)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }
}

struct InteractionsResponse: Decodable { var items: [TaskInteraction] }
struct InteractionSubmission: Codable {
    var revision: Int
    var response_id = UUID().uuidString
    var value: JSONValue
}

enum InteractionAnswer {
    static func value(questions: [InteractionQuestion], selected: [String: [String]], custom: [String: String]) -> JSONValue? {
        var answers: [JSONValue] = []
        for q in questions {
            let choices = selected[q.id] ?? []
            let text = (custom[q.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let options = Set((q.options ?? []).map(\.label))
            guard !choices.isEmpty || !text.isEmpty, text.utf8.count <= 2048,
                  Set(choices).count == choices.count, Set(choices).isSubset(of: options),
                  q.multiSelect == true || choices.count <= 1 else { return nil }
            var a: [String: JSONValue] = ["id": .string(q.id), "selected": .array(choices.map(JSONValue.string))]
            if !text.isEmpty { a["custom"] = .string(text) }
            answers.append(.object(a))
        }
        return questions.isEmpty ? nil : .object(["answers": .array(answers)])
    }
}
