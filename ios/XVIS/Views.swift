import SwiftUI

struct RootView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("xvis.appearance") private var appearance = "system"
    @SceneStorage("xvis.readingTaskID") private var selectedTaskID: String?
    @State private var projectID = ProjectView.defaultWorkspaceID
    @State private var sidebarVisible = false
    @State private var openingSidebar = false
    @State private var sidebarDrag: CGFloat = 0
    @State private var followSubmission = false
    @State private var settingsVisible = false
    @State private var showDiscard = false
    @State private var suggestedDraft: String?
    @State private var replaceDraft = false
    @State private var imageImporting = false
    @State private var projectContextRoute: ProjectContextRoute?
    @FocusState private var inputFocused: Bool
    @AccessibilityFocusState private var historyButtonFocused: Bool

    // The selected ID remains a reading anchor; a continuation always targets the loaded tail.
    private var tailID: String? { selectedTaskID.flatMap { store.conversationTaskIDs(for: $0).last } }
    private var selectedTask: TaskView? { tailID.flatMap { store.state.caches[$0]?.snapshot } }
    private var selectedProject: ProjectView? {
        if projectID == ProjectView.defaultWorkspaceID { return ProjectView.defaultWorkspace(in: store.projects) }
        return store.projects.first { $0.scopedID == projectID && $0.available }
    }
    private var draft: Binding<String> {
        let key = tailID
        return Binding(get: { store.draft(for: key) }, set: { store.setDraft($0, for: key) })
    }
    private var canSend: Bool {
        guard store.hasConnection, !store.isSending, store.state.pending == nil,
              !store.conversationLoading, !imageImporting,
              (!draft.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !store.imageDrafts(for: tailID).isEmpty),
              draft.wrappedValue.utf8.count <= 16384 else { return false }
        if selectedTaskID != nil { return selectedTask?.status == "done" && selectedTask?.can_followup == true }
        return selectedProject != nil
    }
    private var workspaceName: String {
        if let task = selectedTask { return store.projectName(task.project_id, nodeID: task.node_id) }
        return selectedProject?.name ?? (projectID == ProjectView.defaultWorkspaceID ? "测试工作区" : "选择工作区")
    }
    private var motion: Animation? { reduceMotion ? nil : .spring(response: 0.30, dampingFraction: 0.9) }
    private var preferredAppearance: ColorScheme? { appearance == "light" ? .light : appearance == "dark" ? .dark : nil }
    private var connectionLabel: String {
        if !store.hasConnection { return "尚未连接" }
        if store.connectionFailed { return "同步暂不可用" }
        let nodeID = selectedTask?.node_id ?? selectedProject?.node_id
        if let node = store.nodes.first(where: { $0.id == nodeID }) { return node.name + (node.online ? " · 在线" : " · 离线") }
        return "正在同步"
    }

    var body: some View {
        GeometryReader { geometry in
            let width = min(geometry.size.width * 0.88, 370)
            let isPresented = sidebarVisible || openingSidebar
            let progress = sidebarVisible ? max(0, 1 + sidebarDrag / width) : min(1, sidebarDrag / width)
            ZStack(alignment: .leading) {
                chatScreen.accessibilityHidden(isPresented)
                if isPresented {
                    Color.black.opacity(0.26 * progress)
                        .ignoresSafeArea()
                        .contentShape(Rectangle())
                        .onTapGesture { closeSidebar() }
                        .accessibilityLabel("关闭历史记录").accessibilityAddTraits(.isButton)
                    HistorySidebar(selectedTaskID: selectedTaskID, selectTask: openTask,
                                   newConversation: startNewConversation, showSettings: {
                        closeSidebar(); settingsVisible = true
                    }, close: closeSidebar)
                    .frame(width: width)
                    .background(XTheme.surface)
                    .clipShape(UnevenRoundedRectangle(bottomTrailingRadius: 24, topTrailingRadius: 24))
                    .shadow(color: .black.opacity(0.12), radius: 20, x: 6)
                    .offset(x: sidebarVisible ? sidebarDrag : -width + sidebarDrag)
                    .gesture(DragGesture(minimumDistance: 12)
                        .onChanged { value in
                            guard sidebarVisible, abs(value.translation.width) > abs(value.translation.height) else { return }
                            sidebarDrag = max(-width, min(0, value.translation.width))
                        }
                        .onEnded { value in
                            guard sidebarVisible else { return }
                            let close = sidebarDrag < -width * 0.3 || value.predictedEndTranslation.width < -width * 0.5
                            withAnimation(motion) {
                                sidebarDrag = 0
                                if close { sidebarVisible = false }
                            }
                        })
                    .transition(reduceMotion ? .opacity : .move(edge: .leading))
                    .zIndex(1)
                }
            }
            .overlay(alignment: .leading) {
                if !sidebarVisible {
                    Color.clear.frame(width: 22).contentShape(Rectangle())
                        .accessibilityHidden(true)
                        .gesture(DragGesture(minimumDistance: 12)
                            .onChanged { value in
                                guard value.translation.width > 0,
                                      abs(value.translation.width) > abs(value.translation.height) * 1.4 else { return }
                                inputFocused = false
                                openingSidebar = true
                                sidebarDrag = min(width, value.translation.width)
                            }
                            .onEnded { value in
                                guard openingSidebar else { return }
                                let open = sidebarDrag > width * 0.3 || value.predictedEndTranslation.width > width * 0.5
                                withAnimation(motion) {
                                    sidebarVisible = open
                                    openingSidebar = false
                                    sidebarDrag = 0
                                }
                            })
                }
            }
        }
        .background(XTheme.background.ignoresSafeArea())
        .foregroundStyle(XTheme.text)
        .tint(XTheme.accent)
        .preferredColorScheme(preferredAppearance)
        .overlay(alignment: .top) {
            if let message = store.errorMessage {
                ErrorBanner(message: message, retry: {
                    store.errorMessage = nil
                    Task { await store.refreshVisible() }
                }, dismiss: { store.errorMessage = nil })
                .padding(.horizontal, 16).padding(.top, 6)
                .transition(reduceMotion ? .opacity : .move(edge: .top).combined(with: .opacity))
                .zIndex(2)
            }
        }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: store.errorMessage != nil)
        .sheet(isPresented: $settingsVisible) {
            NavigationStack {
                SettingsView(openTask: { id in settingsVisible = false; openTask(id) }).toolbar {
                    ToolbarItem(placement: .topBarTrailing) { Button("完成") { settingsVisible = false } }
                }
            }
            .tint(XTheme.accent).preferredColorScheme(preferredAppearance)
        }
        .sheet(item: $projectContextRoute) { route in
            ProjectContextView(route: route, openTask: { id in projectContextRoute = nil; openTask(id) })
                .preferredColorScheme(preferredAppearance)
        }
        .confirmationDialog("此请求可能已经在服务器执行。放弃仅删除手机的重试记录，不会取消任务。", isPresented: $showDiscard, titleVisibility: .visible) {
            Button("放弃重试记录", role: .destructive) { store.discardPending() }
            Button("保留记录", role: .cancel) { }
        }
        .confirmationDialog("替换当前未发送的草稿？", isPresented: $replaceDraft, titleVisibility: .visible) {
            Button("使用建议指令") { applySuggestion() }
            Button("保留草稿", role: .cancel) { suggestedDraft = nil }
        }
        .onChange(of: store.errorMessage) { _, message in if message != nil { Haptics.failed() } }
        .onChange(of: store.lastConfirmedSubmission) { _, confirmed in
            guard confirmed != nil else { return }
            Haptics.sent()
            if followSubmission, let id = store.submittedTaskID { openTask(id) }
            followSubmission = false
        }
        .onChange(of: store.hasConnection) { _, connected in if !connected { resetConnectionContext() } }
        .onChange(of: store.state.baseURL) { _, _ in resetConnectionContext() }
        .onChange(of: selectedTaskID, initial: true) { _, id in store.activeTaskID = id }
        .onChange(of: store.notificationRoute, initial: true) { _, route in
            guard let route else { return }
            settingsVisible = false
            openTask(route.taskID)
            store.consumeNotificationRoute()
        }
    }

    private var chatScreen: some View {
        VStack(spacing: 0) {
            header
            if !store.pendingInteractions.isEmpty && !sidebarVisible {
                Button { withAnimation(motion) { sidebarVisible = true } } label: {
                    Label("\(store.pendingInteractions.count) 项待我处理", systemImage: "hand.raised")
                        .font(.subheadline.weight(.medium)).frame(maxWidth: .infinity, minHeight: 44)
                }.foregroundStyle(XTheme.accent).background(XTheme.soft)
            }
            if let id = selectedTaskID {
                ConversationView(taskID: id, openTask: openTask, retryTask: retryTask,
                                 dismissKeyboard: { inputFocused = false }).id(id)
            } else { welcome }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if selectedTask?.interactions?.contains(where: { $0.status == "pending" || $0.status == "submitted" }) == true {
                Label(workspaceName, systemImage: "folder")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
                    .frame(maxWidth: .infinity, minHeight: 44).background(XTheme.background)
            } else { composer }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Button {
                inputFocused = false
                withAnimation(motion) { sidebarVisible = true; sidebarDrag = 0 }
            } label: { Image(systemName: "line.3.horizontal").font(.system(size: 20, weight: .medium)) }
            .buttonStyle(XIconButtonStyle())
            .overlay(alignment: .topTrailing) {
                if store.unreadCount > 0 {
                    Text(store.unreadCount > 99 ? "99+" : "\(store.unreadCount)")
                        .font(.system(size: 10, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(XTheme.onAccent)
                        .padding(.horizontal, 4).frame(minWidth: 17, minHeight: 17)
                        .background(XTheme.accent, in: Capsule())
                        .allowsHitTesting(false).accessibilityHidden(true)
                }
            }
            .accessibilityLabel(store.unreadCount > 0 ? "历史记录和设置，\(store.unreadCount) 个结果待查看" : "历史记录和设置")
            .accessibilityFocused($historyButtonFocused)
            Spacer(minLength: 0)
            VStack(spacing: 3) {
                Text("XVIS").font(.headline.weight(.semibold))
                HStack(spacing: 5) {
                    Image(systemName: store.connectionFailed ? "wifi.slash" : "desktopcomputer")
                    Text(connectionLabel)
                }
                .font(.caption).foregroundStyle(store.connectionFailed ? XTheme.warning : XTheme.secondary)
                .accessibilityLabel(connectionLabel + "，手机仅在前台同步")
            }
            Spacer(minLength: 0)
            Button(action: startNewConversation) {
                Image(systemName: "square.and.pencil").font(.system(size: 20, weight: .medium))
            }
            .buttonStyle(XIconButtonStyle()).accessibilityLabel("新对话")
        }
        .padding(.horizontal, 10).padding(.vertical, 7)
        .xControlSurface(cornerRadius: 24)
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<11: return "早上好"
        case 11..<14: return "中午好"
        case 14..<18: return "下午好"
        default: return "晚上好"
        }
    }

    private var welcome: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Spacer(minLength: 28)
                XVISMark(size: 60).padding(.bottom, 24)
                Text(greeting + "，\n想从哪里开始？")
                    .font(.largeTitle.weight(.semibold)).tracking(-0.5)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 12)
                Text(store.hasConnection ? "思考、推进与探索，\n让想法化为行动。" : "连接你的设备，\n把想法变成下一步。")
                    .font(.body).lineSpacing(5).foregroundStyle(XTheme.secondary)
                    .padding(.bottom, 28)
                VStack(spacing: 10) {
                    suggestion("灵感研讨", subtitle: "碰撞想法，拓展思路", icon: "lightbulb",
                               prompt: "和我一起研讨一个想法，帮助我完善思路、比较方向。我想讨论的是：")
                    suggestion("项目推进", subtitle: "梳理进展，明确下一步", icon: "flag",
                               prompt: "帮我梳理项目目标、当前进展和下一步行动。项目情况是：")
                    suggestion("咨询检索", subtitle: "查找资料，解答疑问", icon: "magnifyingglass",
                               prompt: "帮我分析这个问题，必要时检索资料并注明来源。我想了解的是：")
                }
                if !store.hasConnection {
                    Button("设置连接") { settingsVisible = true }
                        .font(.subheadline.weight(.semibold)).frame(minHeight: 44).padding(.top, 16)
                }
                Spacer(minLength: 28)
            }
            .padding(.horizontal, 28)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(minHeight: 500)
            .background { Color.clear.contentShape(Rectangle()).onTapGesture { inputFocused = false } }
        }
        .scrollBounceBehavior(.basedOnSize).scrollDismissesKeyboard(.interactively)
        .refreshable { await store.refreshVisible() }
    }

    private func suggestion(_ title: String, subtitle: String, icon: String, prompt: String) -> some View {
        Button {
            suggestedDraft = prompt
            if draft.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { applySuggestion() }
            else { replaceDraft = true }
        } label: {
            HStack(spacing: 14) {
                Image(systemName: icon).font(.system(size: 21, weight: .regular))
                    .foregroundStyle(XTheme.accent).frame(width: 30)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.body.weight(.medium)).foregroundStyle(XTheme.text)
                    Text(subtitle).font(.caption).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 2)
                Image(systemName: "arrow.up.left").font(.caption.weight(.medium)).foregroundStyle(XTheme.secondary)
            }
            .padding(.horizontal, 16).padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay { RoundedRectangle(cornerRadius: 18).strokeBorder(XTheme.border, lineWidth: 0.75) }
        }
        .buttonStyle(XPressButtonStyle())
        .accessibilityLabel(title + "，" + subtitle)
        .accessibilityHint("填入可编辑的建议指令，不会自动发送")
    }

    private func applySuggestion() {
        guard let text = suggestedDraft else { return }
        draft.wrappedValue = text
        suggestedDraft = nil
        inputFocused = true
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let pending = store.state.pending, !store.isSending {
                PendingSubmissionCard(pending: pending, isSending: store.isSending, retry: {
                    inputFocused = false; followSubmission = true
                    Task { await store.retryPending() }
                }, discard: { showDiscard = true })
            }
            VStack(alignment: .leading, spacing: 10) {
                ImageComposer(taskID: tailID, onProcessingChanged: { imageImporting = $0 })
                TextField(selectedTaskID == nil ? "写下你的想法…" : "继续这段对话…", text: draft, axis: .vertical)
                    .font(.body).lineSpacing(4).lineLimit(1...6)
                    .frame(minHeight: 32, alignment: .top).focused($inputFocused)
                    .accessibilityLabel(selectedTaskID == nil ? "任务内容" : "继续任务内容")
                ViewThatFits(in: .horizontal) {
                    composerActions
                    VStack(alignment: .leading, spacing: 6) {
                        workspaceControl
                        HStack { continuationHint; Spacer(); sendControls }
                    }
                }
            }
            .padding(.horizontal, 16).padding(.top, 16).padding(.bottom, 10)
            .xControlSurface(cornerRadius: 26, active: inputFocused)
            if let hint = composerHint {
                Text(hint).font(.caption).foregroundStyle(XTheme.secondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.horizontal, 6)
            }
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 8)
    }

    private var composerActions: some View {
        HStack(spacing: 8) {
            workspaceControl
            Spacer(minLength: 4)
            sendControls
        }
    }

    private var sendControls: some View {
        HStack(spacing: 4) {
            if inputFocused { DismissKeyboardButton { inputFocused = false } }
            Button(action: send) {
                Group {
                    if store.isSending { ProgressView().tint(XTheme.onAccent) }
                    else { Image(systemName: "arrow.up").font(.body.weight(.semibold)) }
                }
                .frame(width: 44, height: 44)
                .foregroundStyle(canSend || store.isSending ? XTheme.onAccent : XTheme.secondary)
                .background(canSend || store.isSending ? XTheme.accent : XTheme.soft, in: Circle())
            }
            .buttonStyle(XPressButtonStyle()).disabled(!canSend)
            .accessibilityLabel(store.isSending ? "正在发送" : "发送消息")
            .keyboardShortcut(.return, modifiers: .command)
        }
    }

    @ViewBuilder private var workspaceControl: some View {
        if selectedTaskID == nil { workspaceMenu }
        else {
            Menu {
                if let task = selectedTask {
                    Button("项目背景与进度") {
                        inputFocused = false
                        projectContextRoute = ProjectContextRoute(nodeID: task.node_id, projectID: task.project_id)
                    }
                }
            } label: { Label(workspaceName, systemImage: "folder") }
                .font(.subheadline).foregroundStyle(XTheme.accent).lineLimit(1)
                .padding(.vertical, 8).frame(minHeight: 44)
                .accessibilityLabel("当前会话工作区：" + workspaceName)
        }
    }

    private var workspaceMenu: some View {
        Menu {
            Button { projectID = ProjectView.defaultWorkspaceID } label: {
                Label("测试工作区", systemImage: projectID == ProjectView.defaultWorkspaceID ? "checkmark" : "folder")
            }
            ForEach(store.projects.filter { $0.available }, id: \.scopedID) { project in
                Button { projectID = project.scopedID } label: {
                    Label(store.projectName(project.id, nodeID: project.node_id), systemImage: projectID == project.scopedID ? "checkmark" : "folder")
                }
            }
            if let project = selectedProject {
                Divider()
                Button { inputFocused = false; projectContextRoute = ProjectContextRoute(nodeID: project.node_id, projectID: project.id) } label: {
                    Label("项目背景与进度", systemImage: "doc.text.magnifyingglass")
                }
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "folder")
                Text(workspaceName).lineLimit(1)
                Image(systemName: "chevron.down").font(.caption2)
            }
            .font(.subheadline).foregroundStyle(XTheme.accent)
            .padding(.horizontal, 11).padding(.vertical, 8).frame(minHeight: 44)
            .background(XTheme.soft, in: Capsule())
        }
        .accessibilityLabel("工作区：\(workspaceName)，可切换")
    }

    private var continuationHint: some View {
        Text(selectedTask?.status == "done" && selectedTask?.can_followup == true ? "可继续追问" : "草稿将保留")
            .font(.caption).foregroundStyle(XTheme.secondary)
    }

    private var composerHint: String? {
        if draft.wrappedValue.utf8.count > 16384 { return "内容较长，请缩短后再发送。" }
        if !store.hasConnection { return "请先设置服务器连接。" }
        if store.isSending { return "正在确认发送，你可以继续准备下一条草稿。" }
        if store.state.pending != nil { return nil }
        if store.connectionFailed { return "同步暂不可用，已有记录和草稿仍保留。" }
        if store.conversationLoading { return "正在补齐这段对话…" }
        if selectedTaskID == nil {
            if selectedProject == nil { return "\(workspaceName)暂不可用，请等待执行设备同步或手动选择其他工作区。" }
            if let project = selectedProject, store.nodes.first(where: { $0.id == project.node_id })?.online == false {
                return "执行设备暂时离线，发送后会等待上线执行。"
            }
        } else if let task = selectedTask {
            if !task.terminal { return "本轮结束后可继续发送，你可以先准备草稿。" }
            if !task.can_followup { return "本轮暂不能续接，可以新建对话继续。" }
        }
        return nil
    }

    private func send() {
        guard canSend else { return }
        let text = draft.wrappedValue
        inputFocused = false; followSubmission = true
        if let id = tailID { Task { await store.followup(taskID: id, message: text) } }
        else if let project = selectedProject { Task { await store.submit(nodeID: project.node_id, projectID: project.id, prompt: text) } }
    }
    private func retryTask(_ task: TaskView) {
        guard task.prompt != nil else { return }
        inputFocused = false; followSubmission = true
        Task { await store.retryTaskWithImages(task) }
    }
    private func openTask(_ id: String) {
        inputFocused = false; followSubmission = false; selectedTaskID = id
        closeSidebar()
    }
    private func startNewConversation() {
        inputFocused = false; followSubmission = false; selectedTaskID = nil
        projectID = ProjectView.defaultWorkspaceID; closeSidebar()
    }
    private func resetConnectionContext() {
        selectedTaskID = nil; projectID = ProjectView.defaultWorkspaceID; inputFocused = false
        closeSidebar()
    }
    private func closeSidebar() {
        let wasVisible = sidebarVisible || openingSidebar
        withAnimation(motion) { sidebarVisible = false; openingSidebar = false; sidebarDrag = 0 }
        if wasVisible { historyButtonFocused = true }
    }
}

private struct PendingSubmissionCard: View {
    let pending: PendingSubmission
    let isSending: Bool
    let retry: () -> Void
    let discard: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("上次发送尚未确认", systemImage: "exclamationmark.circle")
                .font(.subheadline.weight(.medium)).foregroundStyle(XTheme.warning)
            Text(pending.content).font(.caption).foregroundStyle(XTheme.text).lineLimit(3)
            if !pending.imageIDs.isEmpty { Text("包含 \(pending.imageIDs.count) 张图片").font(.caption).foregroundStyle(XTheme.secondary) }
            Text("原样重试会找回同一任务，不会重复新建。")
                .font(.caption).foregroundStyle(XTheme.secondary)
            ViewThatFits(in: .horizontal) {
                HStack { pendingActions }
                VStack(alignment: .leading, spacing: 2) { pendingActions }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(14)
        .background(XTheme.warningBackground, in: RoundedRectangle(cornerRadius: 18))
        .accessibilityElement(children: .contain)
    }
    @ViewBuilder private var pendingActions: some View {
        Button(isSending ? "正在确认…" : "原样重试", action: retry).frame(minHeight: 44)
        Button("放弃手机重试记录", role: .destructive, action: discard).frame(minHeight: 44)
    }
}

struct DismissKeyboardButton: View {
    let dismiss: () -> Void
    var body: some View {
        Button(action: dismiss) {
            Image(systemName: "keyboard.chevron.compact.down").font(.body).frame(width: 44, height: 44)
        }
        .buttonStyle(XIconButtonStyle()).accessibilityLabel("收起键盘")
    }
}

struct SettingsView: View {
    var openTask: (String) -> Void = { _ in }
    @EnvironmentObject private var store: AppStore
    @AppStorage("xvis.appearance") private var appearance = "system"
    @AppStorage("xvis.haptics") private var haptics = true
    @State private var address = ""
    @State private var token = ""
    @State private var checking = false
    @State private var showClear = false
    @State private var assistantSettingsVisible = false
    @State private var devicePairingVisible = false
    @State private var projectContextRoute: ProjectContextRoute?
    private enum Field: Hashable { case address, token }
    @FocusState private var focusedField: Field?
    var body: some View {
        Form {
            Section("助理") {
                Button { assistantSettingsVisible = true } label: {
                    HStack {
                        Label("记忆与人格", systemImage: "person.crop.circle")
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(XTheme.secondary)
                    }.frame(minHeight: 44)
                }.disabled(!store.hasConnection)
                Text("设置助理的称呼、沟通偏好，以及你希望它记住的事。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
            }.listRowBackground(XTheme.surface)
            Section("项目背景与进度") {
                ForEach(store.projects, id: \.scopedID) { project in
                    Button {
                        projectContextRoute = ProjectContextRoute(nodeID: project.node_id, projectID: project.id)
                    } label: {
                        HStack {
                            Label(store.projectName(project.id, nodeID: project.node_id), systemImage: "folder")
                            Spacer()
                            Image(systemName: "chevron.right").font(.caption).foregroundStyle(XTheme.secondary)
                        }.frame(minHeight: 44)
                    }
                }
                if store.projects.isEmpty { Text("连接中枢后，已登记的工作区会显示在这里。").font(.footnote).foregroundStyle(XTheme.secondary) }
                Text("按工作区维护目标、决定、进度和下一步，保存后用于新对话。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
            }.listRowBackground(XTheme.surface)
            Section("外观与反馈") {
                Picker("界面外观", selection: $appearance) {
                    Text("跟随系统").tag("system")
                    Text("暖纸 · 浅色").tag("light")
                    Text("石墨 · 深色").tag("dark")
                }
                Toggle("触觉反馈", isOn: $haptics)
            }.listRowBackground(XTheme.surface)
            NotificationSettingsSection()
            Section("执行设备") {
                Button { devicePairingVisible = true } label: { Label("设备连接", systemImage: "desktopcomputer.and.arrow.down") }
                    .disabled(!store.hasConnection)
            }.listRowBackground(XTheme.surface)
            Section("服务器连接") {
                TextField("HTTPS 服务器地址", text: $address).keyboardType(.URL).textContentType(.URL)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().focused($focusedField, equals: .address)
                SecureField(store.hasConnection ? "留空保留当前访问凭证" : "手机访问凭证", text: $token)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().focused($focusedField, equals: .token)
                Button(checking ? "正在检测…" : "检测并保存连接") {
                    checking = true
                    Task { await store.connect(baseURL: address, newToken: token); checking = false; token = "" }
                }.disabled(checking || store.isSending)
                if let note = store.connectionNote { Text(note).font(.footnote).foregroundStyle(XTheme.secondary) }
            }.listRowBackground(XTheme.surface)
            Section {
                Text("退出或锁屏后，执行设备会继续完成任务。开启通知后可接收提醒；漏收的结果也会保留在侧栏，再次打开即可查看。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
                Text("访问凭证保存在此手机的系统钥匙串中，不会同步到其他设备。")
                    .font(.footnote).foregroundStyle(XTheme.secondary)
                if store.hasConnection { Button("清除连接", role: .destructive) { showClear = true }.disabled(store.isSending) }
            }.listRowBackground(XTheme.surface)
        }
        .scrollContentBackground(.hidden).background(XTheme.background).foregroundStyle(XTheme.text)
        .scrollDismissesKeyboard(.interactively)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) { Spacer(); Button("收起键盘") { focusedField = nil } }
        }
        .navigationTitle("设置")
        .sheet(isPresented: $assistantSettingsVisible) { AssistantSettingsView() }
        .sheet(isPresented: $devicePairingVisible) { DevicePairingView() }
        .sheet(item: $projectContextRoute) { route in
            ProjectContextView(route: route, openTask: { id in projectContextRoute = nil; openTask(id) })
        }
        .onAppear { address = store.state.baseURL }
        .confirmationDialog("清除访问凭证和手机记录？服务器上的任务不会被取消。", isPresented: $showClear, titleVisibility: .visible) {
            Button("清除连接", role: .destructive) { store.clearConnection(); token = "" }
            Button("保留连接", role: .cancel) { }
        }
    }
}

private struct ErrorBanner: View {
    let message: String
    let retry: () -> Void
    let dismiss: () -> Void
    @State private var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "exclamationmark.triangle").foregroundStyle(XTheme.warning).padding(.top, 12)
                    .accessibilityHidden(true)
                Text(message).font(.subheadline).foregroundStyle(XTheme.text).lineLimit(expanded ? nil : 3)
                    .fixedSize(horizontal: false, vertical: true).padding(.vertical, 10)
                Spacer(minLength: 0)
                Button(action: dismiss) { Image(systemName: "xmark") }.buttonStyle(XIconButtonStyle())
                    .accessibilityLabel("关闭提示")
            }
            ViewThatFits(in: .horizontal) {
                HStack { recoveryActions }
                VStack(alignment: .leading) { recoveryActions }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 4)
        .background(XTheme.elevated, in: RoundedRectangle(cornerRadius: 18))
        .overlay { RoundedRectangle(cornerRadius: 18).strokeBorder(XTheme.border, lineWidth: 0.75) }
        .shadow(color: .black.opacity(0.10), radius: 12, y: 4)
    }
    @ViewBuilder private var recoveryActions: some View {
        Button("重试同步", action: retry).frame(minHeight: 44)
        Button(expanded ? "收起详情" : "展开详情") { expanded.toggle() }.frame(minHeight: 44)
        Button("复制") { UIPasteboard.general.string = message }.frame(minHeight: 44).accessibilityLabel("复制错误信息")
    }
}

enum Haptics {
    private static var enabled: Bool { UserDefaults.standard.object(forKey: "xvis.haptics") as? Bool ?? true }
    static func sent() { if enabled { UIImpactFeedbackGenerator(style: .light).impactOccurred() } }
    static func completed() { if enabled { UINotificationFeedbackGenerator().notificationOccurred(.success) } }
    static func failed() { if enabled { UINotificationFeedbackGenerator().notificationOccurred(.error) } }
}

func relativeTime(_ text: String) -> String? {
    guard let date = taskDate(text) else { return nil }
    let formatter = RelativeDateTimeFormatter(); formatter.dateTimeStyle = .named
    return formatter.localizedString(for: date, relativeTo: Date())
}
func taskTitle(_ task: TaskView) -> String {
    let title = task.title?.trimmingCharacters(in: .whitespacesAndNewlines)
    return title.flatMap { $0.isEmpty ? nil : $0 } ?? "项目任务"
}
func taskDate(_ text: String) -> Date? {
    let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.date(from: text) ?? ISO8601DateTimeFallback.date(from: text)
}
private let ISO8601DateTimeFallback = ISO8601DateFormatter()
