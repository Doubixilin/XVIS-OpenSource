import Foundation

struct ProjectContext: Codable, Equatable {
    var schema_version: Int?
    var revision = 0
    var goal = ""
    var constraints = ""
    var materials = ""
    var decisions = ""
    var progress = ""
    var open_questions = ""
    var next_steps = ""
    var source_task_ids: [String] = []
    var node_id: String?
    var project_id: String?
    var updated_at: String?
    var sources: [ProjectSource]?

    init() { }
    private enum CodingKeys: String, CodingKey {
        case schema_version, revision, goal, constraints, materials, decisions, progress, open_questions, next_steps
        case source_task_ids, node_id, project_id, updated_at, sources
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let populated = !c.allKeys.isEmpty
        schema_version = try c.decodeIfPresent(Int.self, forKey: .schema_version)
        revision = populated ? try c.decode(Int.self, forKey: .revision) : 0
        goal = populated ? try c.decode(String.self, forKey: .goal) : ""
        constraints = populated ? try c.decode(String.self, forKey: .constraints) : ""
        materials = populated ? try c.decode(String.self, forKey: .materials) : ""
        decisions = populated ? try c.decode(String.self, forKey: .decisions) : ""
        progress = populated ? try c.decode(String.self, forKey: .progress) : ""
        open_questions = populated ? try c.decode(String.self, forKey: .open_questions) : ""
        next_steps = populated ? try c.decode(String.self, forKey: .next_steps) : ""
        source_task_ids = populated ? try c.decode([String].self, forKey: .source_task_ids) : []
        node_id = try c.decodeIfPresent(String.self, forKey: .node_id)
        project_id = try c.decodeIfPresent(String.self, forKey: .project_id)
        updated_at = try c.decodeIfPresent(String.self, forKey: .updated_at)
        sources = try c.decodeIfPresent([ProjectSource].self, forKey: .sources)
    }

    var editable: EditableProjectContext {
        EditableProjectContext(revision: revision, goal: goal, constraints: constraints, materials: materials,
                               decisions: decisions, progress: progress, open_questions: open_questions,
                               next_steps: next_steps, source_task_ids: source_task_ids)
    }
    var validationMessage: String? { editable.validationMessage }
    static func key(nodeID: String, projectID: String) -> String { nodeID + "/" + projectID }
}

struct EditableProjectContext: Codable, Equatable {
    var revision: Int
    var goal: String
    var constraints: String
    var materials: String
    var decisions: String
    var progress: String
    var open_questions: String
    var next_steps: String
    var source_task_ids: [String]
    var validationMessage: String? {
        guard revision >= 0 else { return "项目背景版本无效，请重新载入。" }
        let entries = [("目标", goal), ("约束", constraints), ("资料位置", materials), ("已确认决定", decisions),
                       ("当前进度", progress), ("未决问题", open_questions), ("下一步", next_steps)]
        if let long = entries.first(where: { $0.1.utf8.count > 4096 }) { return "\(long.0)较长，请缩短后保存（每项最多 4 KB）。" }
        guard source_task_ids.count <= 10, Set(source_task_ids).count == source_task_ids.count,
              source_task_ids.allSatisfy(XNotificationID.safeTask) else { return "最多选择十个有效的来源记录。" }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.withoutEscapingSlashes]
        guard let data = try? encoder.encode(self), data.count <= 12 * 1024 else { return "项目背景较长，请精简后保存（总共最多 12 KB）。" }
        return nil
    }
}

struct ProjectSource: Codable, Identifiable, Equatable {
    var task_id: String
    var title: String?
    var status: String
    var created_at: String
    var finished_at: String?
    var prompt: String?
    var text: String?
    var truncated: Bool
    var id: String { task_id }
}
struct ProjectSourcesResponse: Decodable { var sources: [ProjectSource] }
struct ProjectSummaryResponse: Decodable {
    var task_id: String
    var status: String
    var text: String?
    var source_task_ids: [String]
    var truncated: Bool
}
struct ProjectContextRoute: Identifiable {
    var nodeID: String
    var projectID: String
    var sourceTaskID: String?
    var id: String { ProjectContext.key(nodeID: nodeID, projectID: projectID) }
}
struct ProjectEditDraft: Codable, Equatable {
    var context: ProjectContext
    var baseline: EditableProjectContext
    var summaryRequestID: String?
    var summarySourceIDs: [String]?
    var summaryTaskID: String?
}
