import Foundation

struct AssistantMemory: Codable, Identifiable, Equatable {
    var id = "m-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    var text = ""
    var enabled = true
}

struct AssistantProfile: Codable, Equatable {
    var revision = 0
    var name = "XVIS"
    var persona_enabled = false
    var persona = ""
    var memory_enabled = false
    var memories: [AssistantMemory] = []

    var validationMessage: String? {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || name.unicodeScalars.count > 40 || name.utf8.count > 160 { return "助理名称需要 1–40 个字符。" }
        if persona.utf8.count > 4096 { return "人格说明较长，请缩短后保存（最多 4 KB）。" }
        if memories.count > 20 { return "最多保存 20 条记忆。" }
        if Set(memories.map(\.id)).count != memories.count { return "记忆编号重复，请重新载入资料。" }
        for memory in memories {
            if memory.id.range(of: "^m-[0-9a-f]{32}$", options: .regularExpression) == nil { return "记忆编号无效，请重新载入资料。" }
            if memory.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "请填写记忆内容，或移除空条目。" }
            if memory.text.utf8.count > 2048 { return "单条记忆较长，请缩短后保存（最多 2 KB）。" }
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        guard let data = try? encoder.encode(self), data.count <= 16 * 1024 else {
            return "资料总量较大，请精简后保存（最多 16 KB）。"
        }
        return nil
    }
}
