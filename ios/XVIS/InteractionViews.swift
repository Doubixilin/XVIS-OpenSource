import SwiftUI

struct ContinuousControlCard: View {
    let task: TaskView
    let control: TaskControl
    @EnvironmentObject private var store: AppStore
    @State private var editing: ControlItem?
    @State private var composing = false
    @State private var busy = false
    @State private var note: String?
    private var saved: ControlCommand? { store.state.controlSubmissions[task.id] }
    private var enabled: Bool { task.status == "running" && !busy && saved == nil }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !task.terminal {
                Button { editing = nil; composing = true } label: {
                    Label("补充方向或安排下一步", systemImage: "text.badge.plus")
                        .font(.subheadline.weight(.medium)).frame(minHeight: 44)
                }.disabled(!enabled).buttonStyle(XPressButtonStyle())
                Text("补充在下一个思考步骤生效；正在执行的工具仍需完成或单独停止。")
                    .font(.caption).foregroundStyle(XTheme.secondary)
            }
            if let saved {
                Text("有一条提交尚未确认，重试会沿用同一标识。").font(.footnote)
                HStack {
                    Button("原样重试") { send(saved) }.disabled(busy)
                    Button("放弃本地重试") { store.discardControl(taskID: task.id) }.disabled(busy)
                }.font(.footnote).buttonStyle(.bordered)
            }
            ForEach(control.items) { item in
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.delivery == "steer" ? "等待补充到当前轮" : "等待下一轮").font(.caption).foregroundStyle(XTheme.secondary)
                    Text(item.text).font(.subheadline).lineLimit(4).textSelection(.enabled)
                    if enabled {
                        ViewThatFits {
                            HStack(spacing: 18) { actions(item) }
                            VStack(alignment: .leading) { actions(item) }
                        }
                    }
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(XTheme.soft, in: RoundedRectangle(cornerRadius: 12))
            }
            if let note { Text(note).font(.footnote).foregroundStyle(XTheme.warning) }
            if let commands = control.commands, !commands.isEmpty {
                DisclosureGroup("补充记录 · \(commands.count)") {
                    ForEach(commands) { receipt in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(receipt.label).font(.caption.weight(.medium)).foregroundStyle(XTheme.secondary)
                            if let text = receipt.text { Text(text).font(.footnote).textSelection(.enabled) }
                            else { Text(receipt.action == "remove" ? "移除待执行指令" : "提前执行待办指令").font(.footnote) }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
                    }
                }.font(.footnote)
            }
        }
        .tint(XTheme.accent)
        .sheet(isPresented: $composing) { ControlComposer(task: task, control: control, item: editing) }
    }
    @ViewBuilder private func actions(_ item: ControlItem) -> some View {
        Button("编辑") { editing = item; composing = true }
        if item.delivery == "queue" {
            Button("用于当前轮") { send(.init(epoch: control.epoch, action: "promote", item_id: item.id, revision: control.revision)) }
        }
        Button("移除", role: .destructive) { send(.init(epoch: control.epoch, action: "remove", item_id: item.id, revision: control.revision)) }
    }
    private func send(_ command: ControlCommand) {
        busy = true; note = nil
        Task {
            defer { busy = false }
            do { try await store.submitControl(taskID: task.id, command: command) }
            catch { note = error.localizedDescription }
        }
    }
}

private struct ControlComposer: View {
    let task: TaskView
    let control: TaskControl
    let item: ControlItem?
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var mode = "steer"
    @State private var sending = false
    @State private var note: String?
    private var draftID: String { "control-" + task.id + (item?.id ?? "") }
    var body: some View {
        NavigationStack {
            Form {
                if item == nil {
                    Picker("何时执行", selection: $mode) {
                        Text("当前轮").tag("steer")
                        Text("下一轮").tag("queue")
                    }
                }
                Section(item == nil ? "补充内容" : "编辑待执行指令") {
                    TextEditor(text: $text).frame(minHeight: 180).disabled(sending)
                        .accessibilityLabel("补充指令内容")
                }
                Text(mode == "queue" ? "当前轮结束后继续执行，共用本任务的工作区、资料和总时限。" : "在下一个模型步骤补充方向；不会跳过待批准操作。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
                if let note { Text(note).foregroundStyle(XTheme.warning) }
                Button(sending ? "正在发送…" : "发送补充") {
                    sending = true; note = nil
                    Task {
                        defer { sending = false }
                        do {
                            try await store.submitControl(taskID: task.id, command: .init(epoch: control.epoch,
                                action: item == nil ? mode : "edit", text: text, item_id: item?.id, revision: item == nil ? nil : control.revision))
                            store.setDraft("", for: draftID); dismiss()
                        } catch { note = error.localizedDescription }
                    }
                }.disabled(sending || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text.utf8.count > 16384)
            }
            .scrollContentBackground(.hidden).background(XTheme.background).tint(XTheme.accent)
            .navigationTitle(item == nil ? "继续推进" : "编辑指令").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } } }
            .onAppear { mode = item?.delivery ?? "steer"; let saved = store.draft(for: draftID); text = saved.isEmpty ? item?.text ?? "" : saved }
            .onChange(of: text) { _, value in store.setDraft(value, for: draftID) }
        }
    }
}

struct InteractionCard: View {
    let item: TaskInteraction
    @EnvironmentObject private var store: AppStore
    @State private var selected: [String: [String]] = [:]
    @State private var custom: [String: String] = [:]
    @State private var sending = false
    @State private var note: String?
    private var saved: InteractionSubmission? { store.state.interactionSubmissions[item.id] }
    private var editable: Bool { item.canRespond && saved == nil && !sending }
    private var answer: JSONValue? {
        InteractionAnswer.value(questions: item.request.questions ?? [], selected: selected, custom: custom)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(item.kind == "approval" ? "需要批准" : "需要你补充", systemImage: item.kind == "approval" ? "hand.raised" : "text.bubble")
                .font(.headline).foregroundStyle(item.kind == "approval" ? XTheme.warning : XTheme.accent)
            Text(item.label).font(.subheadline).foregroundStyle(XTheme.secondary)
                .accessibilityIdentifier("interaction-status")
            if item.kind == "approval" {
                Text(item.request.reason ?? "").fixedSize(horizontal: false, vertical: true)
                Text("工具：\(item.request.tool_name ?? "")").font(.footnote.weight(.medium))
                Text(item.argumentsText).font(.system(.footnote, design: .monospaced))
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                    .background(XTheme.background, in: RoundedRectangle(cornerRadius: 12))
                Text("仅对上方这一次操作生效。批准后的执行结果会另行显示。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
                if editable {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 16) { approvalButtons }
                        VStack(alignment: .leading, spacing: 4) { approvalButtons }
                    }
                }
            } else {
                ForEach(item.request.questions ?? []) { q in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(q.question).font(.body.weight(.medium)).fixedSize(horizontal: false, vertical: true)
                        if q.multiSelect == true { Text("可多选").font(.caption).foregroundStyle(XTheme.secondary) }
                        ForEach(q.options ?? [], id: \.label) { option in
                            Button {
                                var choices = selected[q.id] ?? []
                                if choices.contains(option.label) { choices.removeAll { $0 == option.label } }
                                else if q.multiSelect == true { choices.append(option.label) }
                                else { choices = [option.label] }
                                selected[q.id] = choices
                            } label: {
                                HStack(alignment: .top, spacing: 10) {
                                    Image(systemName: (selected[q.id] ?? []).contains(option.label) ? "checkmark.circle.fill" : "circle")
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(option.label)
                                        if let description = option.description { Text(description).font(.footnote).foregroundStyle(XTheme.secondary) }
                                    }
                                    Spacer(minLength: 0)
                                }.frame(minHeight: 44, alignment: .leading)
                            }
                            .disabled(!editable).buttonStyle(.plain).foregroundStyle(XTheme.accent)
                        }
                        if editable {
                            TextField("补充你的想法，也可以直接在这里回答", text: Binding(get: { custom[q.id] ?? "" }, set: { custom[q.id] = $0 }), axis: .vertical)
                                .lineLimit(2...6).padding(12)
                                .background(XTheme.background, in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                }
                if editable {
                    Button("发送答复") { if let answer { submit(answer) } }
                        .disabled(answer == nil).frame(minHeight: 44).buttonStyle(.borderedProminent).tint(XTheme.accent)
                }
            }
            if let saved, item.status == "pending" {
                Text("上次答复尚未确认。重试会发送原来的内容，不会新增决定。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
                Button("原样重试答复") { submit(saved.value) }.disabled(sending || !item.canRespond)
                    .frame(minHeight: 44).tint(XTheme.accent)
            }
            if let response = item.response, item.status != "pending" {
                Text(responseSummary(response)).font(.footnote).foregroundStyle(XTheme.secondary)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if sending { ProgressView("正在发送…").tint(XTheme.accent) }
            if let note { Text(note).font(.footnote).foregroundStyle(XTheme.warning).fixedSize(horizontal: false, vertical: true) }
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 18))
        .overlay { RoundedRectangle(cornerRadius: 18).strokeBorder(XTheme.border, lineWidth: 1) }
        .onChange(of: item.status) { _, _ in note = nil }
    }
    @ViewBuilder private var approvalButtons: some View {
        Button("允许本次") { submit(.string("allowed-once")) }
            .frame(minHeight: 44).buttonStyle(.borderedProminent).tint(XTheme.accent)
        Button("拒绝本次", role: .destructive) { submit(.string("rejected")) }
            .frame(minHeight: 44).buttonStyle(.bordered)
    }
    private func submit(_ value: JSONValue) {
        sending = true; note = nil
        Task {
            do { try await store.respondToInteraction(item, value: value) }
            catch { note = error.localizedDescription }
            sending = false
        }
    }
    private func responseSummary(_ value: JSONValue) -> String {
        if case .string(let decision) = value { return decision == "allowed-once" ? "你的决定：允许本次" : "你的决定：拒绝本次" }
        guard case .object(let object) = value, case .array(let answers) = object["answers"] else { return "答复已记录" }
        return answers.compactMap { a -> String? in
            guard case .object(let fields) = a else { return nil }
            var parts: [String] = []
            if case .array(let selected) = fields["selected"] { parts += selected.compactMap { if case .string(let s) = $0 { return s }; return nil } }
            if case .string(let custom) = fields["custom"] { parts.append(custom) }
            return parts.joined(separator: "、")
        }.joined(separator: "\n")
    }
}

struct PendingInteractionsView: View {
    let openTask: (String) -> Void
    @EnvironmentObject private var store: AppStore
    var body: some View {
        if !store.pendingInteractions.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Label("待我处理 · \(store.pendingInteractions.count)", systemImage: "hand.raised")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(XTheme.accent)
                ForEach(store.pendingInteractions) { item in
                    Button { openTask(item.task_id) } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.kind == "approval" ? (item.request.reason ?? "需要批准") : (item.request.questions?.first?.question ?? "需要补充"))
                                .font(.subheadline).lineLimit(2)
                            Text(item.label).font(.caption).foregroundStyle(XTheme.secondary)
                        }.frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).padding(.vertical, 5)
                    }.buttonStyle(.plain)
                }
            }.padding(12).background(XTheme.soft, in: RoundedRectangle(cornerRadius: 14))
        }
    }
}
