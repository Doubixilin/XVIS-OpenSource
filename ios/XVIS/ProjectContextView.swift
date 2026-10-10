import SwiftUI

struct ProjectContextView: View {
    let route: ProjectContextRoute
    var openTask: (String) -> Void = { _ in }
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @ScaledMetric(relativeTo: .body) private var editorHeight: CGFloat = 112
    @FocusState private var focused: String?
    @State private var draft = ProjectContext()
    @State private var baseline = ProjectContext().editable
    @State private var initialized = false
    @State private var loading = false
    @State private var saving = false
    @State private var generating = false
    @State private var errorMessage: String?
    @State private var note: String?
    @State private var showSources = false
    @State private var showReload = false
    @State private var showExit = false
    @State private var summaryRequestID: String?
    @State private var summarySourceIDs: [String]?
    @State private var summaryTaskID: String?
    @State private var candidate: ProjectSummaryResponse?
    @State private var candidateEditor = ""
    @State private var editingCandidate = false
    @State private var adopting = false
    private var edited: Bool { draft.editable != baseline }
    private var canSave: Bool { initialized && edited && !loading && !saving && draft.validationMessage == nil }
    private var scopeName: String { store.projectName(route.projectID, nodeID: route.nodeID) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(scopeName).font(.headline)
                    Text("保存后用于这个工作区的新对话。已有会话继续沿用开始时的背景。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                    Text(draft.updated_at.map { "版本 \(draft.revision) · \(relativeTime($0) ?? "时间未确认")更新" } ?? "还没有保存的项目背景")
                        .font(.caption).foregroundStyle(XTheme.secondary)
                }.listRowBackground(XTheme.surface)
                statusSection
                summarySection
                field("目标", key: "goal", text: $draft.goal, hint: "希望这个项目最终达成什么。")
                field("约束", key: "constraints", text: $draft.constraints, hint: "需要遵守的范围、限制与验收要求。")
                field("资料位置", key: "materials", text: $draft.materials, hint: "记录文件或资料的位置；不会因此新增目录访问权限。")
                field("已确认决定", key: "decisions", text: $draft.decisions, hint: "已经确定、不必反复讨论的事项。")
                field("当前进度", key: "progress", text: $draft.progress, hint: "目前完成了什么，以及实际检查结果。")
                field("未决问题", key: "open_questions", text: $draft.open_questions, hint: "尚需核对、选择或补充的信息。")
                field("下一步", key: "next_steps", text: $draft.next_steps, hint: "后续工作顺序和准备事项。")
                Section("来源记录 · \(draft.source_task_ids.count)/10") {
                    ForEach(draft.source_task_ids, id: \.self) { id in
                        Button { if persistDraft() { openTask(id) } } label: {
                            Label(draft.sources?.first(where: { $0.id == id })?.title ?? "查看原任务 \(id)", systemImage: "arrow.up.forward.square")
                                .fixedSize(horizontal: false, vertical: true)
                        }.frame(minHeight: 44).accessibilityLabel("查看项目背景来源任务 \(id)")
                    }
                    Button { focused = nil; showSources = true } label: { Label("选择或检索来源", systemImage: "magnifyingglass") }
                        .frame(minHeight: 44)
                    Text("来源对应整份项目背景，保留原始记录方便核对。摘要不会自动保存，也不代替当前代码和环境检查。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                }.listRowBackground(XTheme.surface)
                if let validation = draft.validationMessage {
                    Section { Label(validation, systemImage: "exclamationmark.circle").foregroundStyle(XTheme.warning) }
                        .listRowBackground(XTheme.warningBackground)
                }
            }
            .scrollContentBackground(.hidden).background(XTheme.background).foregroundStyle(XTheme.text)
            .scrollDismissesKeyboard(.interactively).disabled(saving)
            .navigationTitle("项目背景与进度").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("完成") { focused = nil; if edited { showExit = true } else { dismiss() } } }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(saving ? "正在保存…" : "保存") { Task { await save() } }.disabled(!canSave)
                        .accessibilityLabel("保存项目背景")
                }
                ToolbarItemGroup(placement: .keyboard) { Spacer(); Button("收起键盘") { focused = nil } }
            }
        }
        .tint(XTheme.accent).interactiveDismissDisabled(saving || edited)
        .task {
            if !initialized { initialize() }
            await load(replacing: false)
        }
        .task(id: "\(summaryTaskID ?? "none")-\(String(describing: scenePhase))") {
            guard let id = summaryTaskID, scenePhase == .active else { return }
            while !Task.isCancelled {
                await refreshCandidate(id)
                if candidate?.status == "done" || ["failed", "cancelled", "timed_out", "interrupted"].contains(candidate?.status ?? "") { break }
                do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { break }
            }
        }
        .onChange(of: draft) { _, _ in if initialized && !saving { persistDraft() } }
        .sheet(isPresented: $showSources) {
            ProjectSourcePicker(route: route, selected: $draft.source_task_ids, known: draft.sources ?? [],
                                openTask: { id in if persistDraft() { openTask(id) } })
        }
        .sheet(isPresented: $editingCandidate) {
            NavigationStack {
                VStack(alignment: .leading, spacing: 12) {
                    Text("摘要已截断，请核对完整任务并编辑后采用；不会自动替换进度。")
                        .font(.footnote).foregroundStyle(XTheme.warning)
                    TextEditor(text: $candidateEditor).font(.body)
                        .accessibilityLabel("编辑模型进度摘要")
                    if candidateEditor.utf8.count > 4096 { Text("最多 4 KB，请缩短后采用。").foregroundStyle(XTheme.error) }
                }.padding(18).background(XTheme.background)
                    .navigationTitle("编辑摘要").navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .topBarLeading) { Button("取消") { editingCandidate = false } }
                        ToolbarItem(placement: .topBarTrailing) {
                            Button("采用到编辑稿") { adopt(candidateEditor); editingCandidate = false }
                                .disabled(candidateEditor.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || candidateEditor.utf8.count > 4096)
                        }
                    }
            }.tint(XTheme.accent)
        }
        .confirmationDialog("重新载入项目背景？", isPresented: $showReload, titleVisibility: .visible) {
            Button("载入最新版本并保留当前文字") { Task { await load(replacing: false, mergeRevision: true) } }
            Button("载入并替换编辑稿", role: .destructive) { Task { await load(replacing: true) } }
            Button("继续编辑", role: .cancel) { }
        } message: { Text("保留文字会更新版本基准，仍需你核对并保存；替换稿会使用中枢内容。") }
        .confirmationDialog("项目背景尚未保存", isPresented: $showExit, titleVisibility: .visible) {
            Button("保留草稿并关闭") { if persistDraft() { dismiss() } }
            Button("放弃编辑稿", role: .destructive) {
                do { try store.persistProjectEditDraft(nil, nodeID: route.nodeID, projectID: route.projectID); dismiss() }
                catch { errorMessage = error.localizedDescription }
            }
            Button("继续编辑", role: .cancel) { }
        } message: { Text("草稿会按工作区保留，只有明确保存后才用于新对话。") }
        .confirmationDialog("用摘要替换当前进度编辑稿？", isPresented: $adopting, titleVisibility: .visible) {
            Button("采用到进度编辑稿") { adopt(candidate?.text ?? "") }
            Button("保留当前进度", role: .cancel) { }
        } message: { Text("其他六项保持原稿，采用后仍需核对并保存。") }
    }

    @ViewBuilder private var statusSection: some View {
        if loading || edited || errorMessage != nil || note != nil {
            Section {
                if loading { ProgressView("正在读取项目背景…").tint(XTheme.accent) }
                if edited { Label("有未保存的更改", systemImage: "pencil.circle").foregroundStyle(XTheme.accent) }
                if let errorMessage { Text(errorMessage).font(.footnote).foregroundStyle(XTheme.error) }
                if let note { Text(note).font(.footnote).foregroundStyle(XTheme.secondary) }
                Button("重新载入项目背景") {
                    focused = nil
                    if edited { showReload = true } else { Task { await load(replacing: true) } }
                }.frame(minHeight: 44).disabled(loading || saving)
            }.listRowBackground(XTheme.surface)
        }
    }
    private func field(_ label: String, key: String, text: Binding<String>, hint: String) -> some View {
        Section {
            TextEditor(text: text).font(.body).scrollContentBackground(.hidden)
                .frame(height: editorHeight).focused($focused, equals: key)
                .accessibilityLabel("项目背景：\(label)")
        } header: { Text(label) } footer: { Text(hint) }.listRowBackground(XTheme.surface)
    }
    private var summarySection: some View {
        Section("从记录生成进度摘要") {
            Button { focused = nil; showSources = true } label: {
                Label("选择来源 · \(draft.source_task_ids.count) 条", systemImage: "checklist")
            }.frame(minHeight: 44)
            Button(generating ? "正在提交…" : summaryButtonLabel) { Task { await generate() } }
                .disabled(generating || draft.source_task_ids.isEmpty || loading)
                .frame(minHeight: 44)
            if let id = summaryTaskID {
                Button("查看摘要原任务") { if persistDraft() { openTask(id) } }.frame(minHeight: 44)
                Button("刷新摘要结果") { Task { await refreshCandidate(id) } }.frame(minHeight: 44)
            }
            if let candidate {
                if candidate.status == "done", let text = candidate.text, !text.isEmpty {
                    Label("模型生成 · 请审阅后采用", systemImage: "text.badge.star")
                        .font(.subheadline.weight(.medium)).foregroundStyle(XTheme.accent)
                    Text(text).font(.subheadline).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    if candidate.truncated {
                        Text("此候选已截断，请查看原任务并编辑后采用。")
                            .font(.footnote).foregroundStyle(XTheme.warning)
                        Button("编辑截断候选") { candidateEditor = text; editingCandidate = true }.frame(minHeight: 44)
                    } else {
                        Button("采用到进度编辑稿") {
                            if draft.progress.isEmpty { adopt(text) } else { adopting = true }
                        }.frame(minHeight: 44)
                    }
                } else {
                    Text(["queued", "assigned", "running"].contains(candidate.status) ? "摘要任务正在等待或执行，稍后会更新候选。" : "摘要未能成功完成，请查看原任务了解原因。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                }
            }
            Text("只根据所选原记录生成。候选采用到当前进度后仍需保存，其他项目字段不会自动改写。")
                .font(.footnote).foregroundStyle(XTheme.secondary)
        }.listRowBackground(XTheme.surface)
    }
    private func initialize() {
        let key = ProjectContext.key(nodeID: route.nodeID, projectID: route.projectID)
        if let saved = store.projectEditDraft(nodeID: route.nodeID, projectID: route.projectID) {
            draft = saved.context; baseline = saved.baseline
            summaryRequestID = saved.summaryRequestID; summarySourceIDs = saved.summarySourceIDs; summaryTaskID = saved.summaryTaskID
            note = saved.context.editable != saved.baseline ? "已恢复这个工作区的编辑稿，尚未保存。" : "已恢复摘要任务，可继续查看候选。"
        } else {
            draft = store.state.projectContexts[key] ?? ProjectContext(); baseline = draft.editable
        }
        initialized = true
    }
    @discardableResult private func persistDraft() -> Bool {
        do {
            let value: ProjectEditDraft? = edited || summaryRequestID != nil ? ProjectEditDraft(context: draft, baseline: baseline, summaryRequestID: summaryRequestID,
                                         summarySourceIDs: summarySourceIDs, summaryTaskID: summaryTaskID) : nil
            try store.persistProjectEditDraft(value, nodeID: route.nodeID, projectID: route.projectID)
            return true
        } catch { errorMessage = error.localizedDescription; return false }
    }
    private func load(replacing: Bool, mergeRevision: Bool = false) async {
        guard !loading, !saving else { return }
        loading = true; errorMessage = nil
        defer { loading = false }
        do {
            let latest = try await store.fetchProjectContext(nodeID: route.nodeID, projectID: route.projectID)
            guard !Task.isCancelled else { return }
            if replacing || !edited {
                draft = latest; baseline = latest.editable
                if let id = route.sourceTaskID, !draft.source_task_ids.contains(id), draft.source_task_ids.count < 10 {
                    draft.source_task_ids.append(id)
                    note = "已载入最新项目背景，并加入本轮来源。核对后保存即可用于新对话。"
                } else { note = replacing ? "已载入最新项目背景。" : nil }
            } else if mergeRevision {
                draft.revision = latest.revision; draft.updated_at = latest.updated_at
                draft.node_id = latest.node_id; draft.project_id = latest.project_id
                baseline = latest.editable
                note = "已载入最新版本并保留编辑内容，请核对与中枢内容的差异后保存。"
            } else if latest.editable != baseline {
                note = "中枢有最新背景，当前编辑稿仍保留。请重新载入并合并后保存。"
            }
            persistDraft()
        } catch { if !(error is CancellationError) { errorMessage = error.localizedDescription } }
    }
    private func save() async {
        guard canSave else { return }
        saving = true; focused = nil; errorMessage = nil
        defer { saving = false }
        do {
            let saved = try await store.saveProjectContext(draft, nodeID: route.nodeID, projectID: route.projectID)
            draft = saved; baseline = saved.editable; note = "已保存，新对话将使用此版本。"
        } catch { if !(error is CancellationError) { errorMessage = error.localizedDescription; persistDraft() } }
    }
    private func generate() async {
        guard !generating, !draft.source_task_ids.isEmpty else { return }
        generating = true; errorMessage = nil
        defer { generating = false }
        let selected = draft.source_task_ids
        if summarySourceIDs == selected, let id = summaryTaskID { await refreshCandidate(id); return }
        if summaryRequestID == nil || summarySourceIDs != selected {
            summaryRequestID = UUID().uuidString; summarySourceIDs = selected; summaryTaskID = nil; candidate = nil
        }
        guard persistDraft() else { return }
        do {
            guard let requestID = summaryRequestID else { throw AppFailure.localSave }
            let id = try await store.createProjectSummary(nodeID: route.nodeID, projectID: route.projectID, requestID: requestID, sourceIDs: selected)
            summaryTaskID = id; persistDraft(); await refreshCandidate(id)
        } catch { if !(error is CancellationError) { errorMessage = error.localizedDescription } }
    }
    private var summaryButtonLabel: String {
        if summarySourceIDs == draft.source_task_ids {
            if summaryTaskID != nil { return "刷新已有摘要" }
            if summaryRequestID != nil { return "原样重试摘要请求" }
        }
        return "生成摘要候选"
    }
    private func refreshCandidate(_ id: String) async {
        do {
            let response = try await store.projectSummary(nodeID: route.nodeID, projectID: route.projectID, taskID: id)
            guard !Task.isCancelled, summaryTaskID == id else { return }
            guard Set(response.source_task_ids) == Set(summarySourceIDs ?? []) else { throw AppFailure.protocolMismatch }
            candidate = response
        } catch { if !(error is CancellationError) { errorMessage = error.localizedDescription } }
    }
    private func adopt(_ text: String) {
        guard let candidate, candidate.status == "done", text.utf8.count <= 4096 else { return }
        let combined = Array(Set(draft.source_task_ids + candidate.source_task_ids)).sorted()
        guard combined.count <= 10 else { errorMessage = "来源合并后超过十条，请先调整来源再采用。"; return }
        draft.progress = text; draft.source_task_ids = combined
        note = "候选已填入当前进度编辑稿，请核对后保存。"; persistDraft()
    }
}

private struct ProjectSourcePicker: View {
    let route: ProjectContextRoute
    @Binding var selected: [String]
    let known: [ProjectSource]
    let openTask: (String) -> Void
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var sources: [ProjectSource] = []
    @State private var loading = false
    @State private var message: String?
    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack {
                        TextField("检索当前项目的原指令或结果", text: $query).submitLabel(.search)
                            .onSubmit { Task { await search() } }
                        Button("检索") { Task { await search() } }.disabled(loading)
                    }
                    Text("已选择 \(selected.count)/10 条，只展示这个项目的终态记录。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                    if loading { ProgressView("正在读取来源…") }
                    if let message { Text(message).font(.footnote).foregroundStyle(XTheme.error) }
                }.listRowBackground(XTheme.surface)
                ForEach(sources) { source in
                    VStack(alignment: .leading, spacing: 8) {
                        Button {
                            if selected.contains(source.id) { selected.removeAll { $0 == source.id } }
                            else if selected.count < 10 { selected.append(source.id) }
                            else { message = "最多选择十条来源，请先取消一条。" }
                        } label: {
                            HStack(alignment: .top, spacing: 10) {
                                Image(systemName: selected.contains(source.id) ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(XTheme.accent)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(source.title ?? "项目任务").font(.subheadline.weight(.medium)).foregroundStyle(XTheme.text)
                                    Text(relativeTime(source.created_at) ?? "时间未确认").font(.caption).foregroundStyle(XTheme.secondary)
                                }
                            }.frame(minHeight: 44)
                        }.buttonStyle(.plain)
                            .accessibilityLabel("\(selected.contains(source.id) ? "已选择" : "选择来源")：\(source.title ?? source.id)")
                        if let text = source.text, !text.isEmpty { Text(text).font(.footnote).foregroundStyle(XTheme.secondary).lineLimit(5) }
                        if source.truncated { Text("这是截断摘录，完整记录请查看原任务。").font(.caption).foregroundStyle(XTheme.warning) }
                        Button("查看原任务") { dismiss(); openTask(source.id) }.font(.footnote).frame(minHeight: 44)
                    }.listRowBackground(XTheme.surface)
                }
                if !loading && sources.isEmpty { Text("此项目没有匹配的终态记录。").foregroundStyle(XTheme.secondary) }
                if !selected.isEmpty {
                    Section("已选来源") {
                        ForEach(selected, id: \.self) { id in
                            HStack {
                                Text((sources + known).first(where: { $0.id == id })?.title ?? id).font(.footnote)
                                Spacer()
                                Button { selected.removeAll { $0 == id } } label: { Image(systemName: "minus.circle") }
                                    .accessibilityLabel("取消来源 \(id)").frame(minWidth: 44, minHeight: 44)
                            }
                        }
                    }.listRowBackground(XTheme.surface)
                }
            }.scrollContentBackground(.hidden).background(XTheme.background)
                .navigationTitle("项目来源").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("完成") { dismiss() } } }
                .task { await search() }
        }.tint(XTheme.accent)
    }
    private func search() async {
        guard !loading else { return }
        loading = true; message = nil
        defer { loading = false }
        do { sources = try await store.projectSources(nodeID: route.nodeID, projectID: route.projectID, query: query) }
        catch { if !(error is CancellationError) { message = error.localizedDescription } }
    }
}

struct ProjectSnapshotView: View {
    let context: ProjectContext?
    let openTask: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List {
                if let context, context.revision > 0 {
                    Section { Text("本轮使用项目背景版本 \(context.revision)")
                        Text("此快照固定于会话开始时，后续背景编辑不会改写它。").font(.footnote).foregroundStyle(XTheme.secondary)
                    }.listRowBackground(XTheme.surface)
                    ForEach([(0, "目标", context.goal), (1, "约束", context.constraints), (2, "资料位置", context.materials),
                             (3, "已确认决定", context.decisions), (4, "当前进度", context.progress),
                             (5, "未决问题", context.open_questions), (6, "下一步", context.next_steps)], id: \.0) { item in
                        if !item.2.isEmpty { Section(item.1) { Text(item.2).textSelection(.enabled) }.listRowBackground(XTheme.surface) }
                    }
                    Section("原始来源") {
                        ForEach(context.source_task_ids, id: \.self) { id in
                            Button("查看原任务 \(id)") { dismiss(); openTask(id) }.frame(minHeight: 44)
                        }
                    }.listRowBackground(XTheme.surface)
                } else {
                    Text("本轮没有带入项目背景。旧任务保持原会话资料，不会补入后来保存的内容。")
                        .foregroundStyle(XTheme.secondary).listRowBackground(XTheme.surface)
                }
            }.scrollContentBackground(.hidden).background(XTheme.background).foregroundStyle(XTheme.text)
                .navigationTitle("本轮项目背景").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("完成") { dismiss() } } }
        }.tint(XTheme.accent)
    }
}
