import SwiftUI

struct AssistantSettingsView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @ScaledMetric(relativeTo: .body) private var personaEditorHeight: CGFloat = 156
    @ScaledMetric(relativeTo: .body) private var memoryEditorHeight: CGFloat = 104
    @FocusState private var focusedField: Field?
    @State private var draft = AssistantProfile()
    @State private var baseline = AssistantProfile()
    @State private var initialized = false
    @State private var loading = false
    @State private var replacingDraft = false
    @State private var saving = false
    @State private var errorMessage: String?
    @State private var statusMessage: String?
    @State private var showExitConfirmation = false
    @State private var showReloadConfirmation = false
    @State private var showDeleteConfirmation = false
    @State private var memoryToDelete: String?

    private enum Field: Hashable { case name, persona, memory(String) }
    private var edited: Bool { draft != baseline }
    private var canSave: Bool { edited && !loading && !saving && draft.validationMessage == nil }

    var body: some View {
        NavigationStack {
            Form {
                statusSection
                nameSection
                personaSection
                memorySection
                explanationSection
                if let validation = draft.validationMessage {
                    Section {
                        Label(validation, systemImage: "exclamationmark.circle")
                            .font(.subheadline).foregroundStyle(XTheme.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .listRowBackground(XTheme.warningBackground)
                }
            }
            .foregroundStyle(XTheme.text)
            .scrollContentBackground(.hidden)
            .background(XTheme.background)
            .scrollDismissesKeyboard(.interactively)
            .disabled(saving || replacingDraft)
            .navigationTitle("记忆与人格")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(XTheme.background, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("完成", action: finish)
                        .frame(minWidth: 44, minHeight: 44)
                        .disabled(saving || replacingDraft)
                        .accessibilityLabel("完成记忆与人格设置")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await save(closeAfterwards: false) }
                    } label: {
                        if saving { ProgressView().tint(XTheme.accent) }
                        else { Text("保存").fontWeight(.semibold) }
                    }
                    .frame(minWidth: 44, minHeight: 44)
                    .disabled(!canSave)
                    .accessibilityLabel(saving ? "正在保存资料" : "保存记忆与人格")
                }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("收起键盘") { focusedField = nil }
                        .frame(minWidth: 44, minHeight: 44)
                        .accessibilityLabel("收起键盘")
                }
            }
        }
        .tint(XTheme.accent)
        .interactiveDismissDisabled(edited || saving || replacingDraft)
        .task {
            if !initialized {
                let cached = store.state.assistantProfile ?? AssistantProfile()
                draft = cached
                baseline = cached
                initialized = true
            }
            await load(replacingEdits: false)
        }
        .onChange(of: draft) { _, _ in
            if edited { statusMessage = nil }
        }
        .confirmationDialog("更改还没有保存", isPresented: $showExitConfirmation, titleVisibility: .visible) {
            Button("保存并关闭") { Task { await save(closeAfterwards: true) } }
                .disabled(!canSave)
            Button("放弃更改", role: .destructive) {
                draft = baseline
                dismiss()
            }
            Button("继续编辑", role: .cancel) { }
        } message: {
            Text("当前编辑内容还没有更新到助理资料。")
        }
        .confirmationDialog("重新载入资料？", isPresented: $showReloadConfirmation, titleVisibility: .visible) {
            Button("重新载入并替换编辑稿", role: .destructive) {
                Task { await load(replacingEdits: true) }
            }
            Button("继续保留编辑稿", role: .cancel) { }
        } message: {
            Text("读取成功后会替换当前未保存的内容；读取失败会保留编辑稿。")
        }
        .confirmationDialog("删除这条记忆？", isPresented: $showDeleteConfirmation, titleVisibility: .visible) {
            Button("删除记忆", role: .destructive) {
                if let id = memoryToDelete {
                    draft.memories.removeAll { $0.id == id }
                    if focusedField == .memory(id) { focusedField = nil }
                }
                memoryToDelete = nil
            }
            Button("取消", role: .cancel) { memoryToDelete = nil }
        } message: {
            Text("保存后，新对话不再使用这条记忆。历史任务仍保留当时使用的资料。")
        }
    }

    @ViewBuilder private var statusSection: some View {
        if loading || saving || edited || errorMessage != nil || statusMessage != nil {
            Section {
                if loading {
                    HStack(spacing: 10) {
                        ProgressView().tint(XTheme.accent).accessibilityHidden(true)
                        Text("正在读取助理资料…").font(.subheadline)
                    }
                    .accessibilityElement(children: .combine)
                }
                if edited {
                    Label("有未保存的更改", systemImage: "pencil.circle")
                        .font(.subheadline).foregroundStyle(XTheme.accent)
                }
                if let statusMessage {
                    Text(statusMessage).font(.subheadline).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let errorMessage {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("暂时无法同步资料", systemImage: "exclamationmark.circle")
                            .font(.subheadline.weight(.medium)).foregroundStyle(XTheme.error)
                        Text(errorMessage).font(.subheadline).foregroundStyle(XTheme.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("编辑内容仍保留。你可以继续修改，或重新载入中枢资料。")
                            .font(.caption).foregroundStyle(XTheme.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Button("重新载入资料", action: requestReload)
                    .frame(minHeight: 44)
                    .disabled(loading || saving)
                    .accessibilityHint(edited ? "先确认是否替换未保存的编辑内容" : "读取中枢保存的最新资料")
            }
            .listRowBackground(XTheme.surface)
        }
    }

    private var nameSection: some View {
        Section {
            TextField("助理名称", text: $draft.name)
                .textInputAutocapitalization(.never)
                .submitLabel(.done)
                .focused($focusedField, equals: .name)
                .onSubmit { focusedField = nil }
                .accessibilityLabel("助理名称，最多四十个字符")
        } header: {
            Text("助理名称")
        } footer: {
            Text("为自己的助理取一个熟悉的名字，最多 40 个字符。")
        }
        .listRowBackground(XTheme.surface)
    }

    private var personaSection: some View {
        Section {
            Toggle("使用自定义人格", isOn: $draft.persona_enabled)
                .frame(minHeight: 44)
            TextEditor(text: $draft.persona)
                .font(.body)
                .scrollContentBackground(.hidden)
                .frame(height: personaEditorHeight)
                .focused($focusedField, equals: .persona)
                .accessibilityLabel("人格说明")
                .accessibilityHint("描述希望助理采用的角色、语气和回答习惯")
        } header: {
            Text("人格")
        } footer: {
            Text("写下希望助理采用的角色、语气与回答习惯。关闭开关会保留说明，方便以后继续使用。")
        }
        .listRowBackground(XTheme.surface)
    }

    private var memorySection: some View {
        Section {
            Toggle("在新对话中使用记忆", isOn: $draft.memory_enabled)
                .frame(minHeight: 44)
            ForEach($draft.memories) { memory in memoryRow(memory) }
            Button(action: addMemory) {
                Label(draft.memories.count >= 20 ? "已达到二十条记忆" : "添加记忆", systemImage: "plus")
                    .frame(minHeight: 44)
            }
            .disabled(draft.memories.count >= 20)
            .accessibilityLabel(draft.memories.count >= 20 ? "已达到二十条记忆上限" : "添加一条记忆")
        } header: {
            Text("记忆 · \(draft.memories.count)/20")
        } footer: {
            Text("记忆由你手动维护，对话不会自动写入。关闭整体开关或停用某条记忆都会保留文字。")
        }
        .listRowBackground(XTheme.surface)
    }

    private func memoryRow(_ memory: Binding<AssistantMemory>) -> some View {
        let id = memory.wrappedValue.id
        let number = (draft.memories.firstIndex { $0.id == id } ?? 0) + 1
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 8) {
                Text("记忆 \(number)").font(.subheadline.weight(.medium))
                Spacer(minLength: 0)
                Button {
                    focusedField = nil
                    memoryToDelete = id
                    showDeleteConfirmation = true
                } label: {
                    Image(systemName: "trash").foregroundStyle(XTheme.error)
                }
                .buttonStyle(XIconButtonStyle())
                .accessibilityLabel("删除第 \(number) 条记忆")
            }
            TextEditor(text: memory.text)
                .font(.body)
                .scrollContentBackground(.hidden)
                .frame(height: memoryEditorHeight)
                .focused($focusedField, equals: .memory(id))
                .accessibilityLabel("第 \(number) 条记忆内容")
            Toggle("启用这条记忆", isOn: memory.enabled)
                .font(.subheadline).frame(minHeight: 44)
                .accessibilityLabel("启用第 \(number) 条记忆")
        }
        .padding(.vertical, 4)
    }

    private var explanationSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 10) {
                Label("保存后用于新对话", systemImage: "bubble.left.and.bubble.right")
                    .font(.subheadline.weight(.medium))
                Text("正在进行的会话会继续使用开始时的资料。修改或删除记忆后，历史任务仍保留当时使用的资料。")
                    .font(.subheadline).foregroundStyle(XTheme.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 4)
        }
        .listRowBackground(XTheme.surface)
    }

    private func addMemory() {
        guard draft.memories.count < 20 else { return }
        let item = AssistantMemory()
        draft.memories.append(item)
        focusedField = .memory(item.id)
    }

    private func finish() {
        focusedField = nil
        if edited { showExitConfirmation = true }
        else { dismiss() }
    }

    private func requestReload() {
        focusedField = nil
        if edited { showReloadConfirmation = true }
        else { Task { await load(replacingEdits: false) } }
    }

    private func load(replacingEdits: Bool) async {
        guard !loading, !saving else { return }
        loading = true
        replacingDraft = replacingEdits
        errorMessage = nil
        defer { loading = false; replacingDraft = false }
        do {
            let latest = try await store.fetchAssistantProfile()
            guard !Task.isCancelled else { return }
            if replacingEdits || !edited {
                draft = latest
                baseline = latest
                statusMessage = replacingEdits ? "已载入最新资料。" : nil
            } else if latest != baseline {
                statusMessage = "中枢资料已刷新，当前编辑稿仍保留。你可以重新载入后继续修改。"
            }
        } catch {
            guard !(error is CancellationError), !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
        }
    }

    private func save(closeAfterwards: Bool) async {
        guard canSave else { return }
        focusedField = nil
        saving = true
        errorMessage = nil
        let submitted = draft
        defer { saving = false }
        do {
            let saved = try await store.saveAssistantProfile(submitted)
            guard !Task.isCancelled else { return }
            draft = saved
            baseline = saved
            statusMessage = "已保存，新对话会使用这些设置。"
            if closeAfterwards { dismiss() }
        } catch {
            guard !(error is CancellationError), !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
        }
    }
}
