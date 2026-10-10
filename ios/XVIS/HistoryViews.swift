import SwiftUI

struct HistorySidebar: View {
    @EnvironmentObject private var store: AppStore
    let selectedTaskID: String?
    let selectTask: (String) -> Void
    let newConversation: () -> Void
    let showSettings: () -> Void
    let close: () -> Void
    @State private var search = ""
    @State private var projectFilter: String?
    @State private var scope: HistoryScope = .all
    @FocusState private var searchFocused: Bool
    @AccessibilityFocusState private var accessibilityFocus: SidebarFocus?
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.verticalSizeClass) private var verticalSizeClass

    private enum SidebarFocus: Hashable { case heading, search }

    private var projectChoices: [String] {
        Set(store.projects.map(\.scopedID) + store.tasks.map { $0.node_id + "|" + $0.project_id }).sorted {
            store.projectName($0).localizedStandardCompare(store.projectName($1)) == .orderedAscending
        }
    }

    private var filterLabel: String { projectFilter.map { store.projectName($0) } ?? "全部工作区" }
    private var scrollsSettings: Bool { dynamicTypeSize.isAccessibilitySize || verticalSizeClass == .compact }

    private var filteredTasks: [TaskView] {
        let term = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return store.tasks.filter { task in
            guard projectFilter == nil || task.node_id + "|" + task.project_id == projectFilter else { return false }
            guard scope != .running || !task.terminal else { return false }
            return term.isEmpty || taskTitle(task).localizedStandardContains(term)
                || store.projectName(task.project_id, nodeID: task.node_id).localizedStandardContains(term)
                || (task.prompt?.localizedStandardContains(term) ?? false)
        }
    }

    private var filteredResults: [InboxItem] {
        let term = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return store.inboxItems.filter { item in
            (projectFilter == nil || item.node_id + "|" + item.project_id == projectFilter)
                && (term.isEmpty || item.title.localizedStandardContains(term)
                    || store.projectName(item.project_id, nodeID: item.node_id).localizedStandardContains(term))
        }
    }

    private var sections: [HistorySection] {
        let calendar = Calendar.current
        let now = Date()
        let today = calendar.startOfDay(for: now)
        let yesterday = calendar.date(byAdding: .day, value: -1, to: today) ?? today
        let weekStart = calendar.date(byAdding: .day, value: -7, to: today) ?? today
        let grouped = Dictionary(grouping: filteredTasks) { task -> HistoryPeriod in
            guard let date = taskDate(task.created_at) else { return .earlier }
            if calendar.isDate(date, inSameDayAs: now) { return .today }
            if calendar.isDate(date, inSameDayAs: yesterday) { return .yesterday }
            if date >= weekStart && date < today { return .pastWeek }
            return .earlier
        }
        return HistoryPeriod.allCases.compactMap { period in
            guard let tasks = grouped[period], !tasks.isEmpty else { return nil }
            return HistorySection(period: period, tasks: tasks)
        }
    }

    private var connectionSummary: String {
        if !store.hasConnection { return "尚未设置连接" }
        if store.connectionFailed { return "中枢请求失败 · 查看连接" }
        if store.nodes.contains(where: \.online) { return "设备在线 · DSH" }
        if store.nodes.isEmpty { return "已保存连接 · 状态待确认" }
        return "设备离线 · 等待上线"
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    searchField
                    workspaceFilter
                    resultScopes
                    Button {
                        searchFocused = false
                        newConversation()
                    } label: {
                        Label("新对话", systemImage: "square.and.pencil")
                            .font(.body.weight(.medium))
                            .foregroundStyle(XTheme.accent)
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 4)
                            .background(XTheme.soft, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                    .buttonStyle(XPressButtonStyle())

                    Text(scope == .unread ? "还没查看的成果和需要处理的问题，集中在这里。刷新可同步最新结果。" : "仅搜索已载入记录，可在下方查看更早的对话。")
                        .font(.footnote)
                        .foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 4)

                    if scope == .unread {
                        Button {
                            Task { await store.refreshInbox() }
                        } label: {
                            Label(store.isRefreshingInbox ? "正在刷新…" : "刷新结果", systemImage: "arrow.clockwise")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(XTheme.accent)
                                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                                .padding(.horizontal, 4)
                        }
                        .buttonStyle(XPressButtonStyle())
                        .disabled(store.isRefreshingInbox || store.isLoadingMoreInbox)
                    }

                    PendingInteractionsView(openTask: selectTask)
                    if scope == .unread { inboxContent } else { historyContent }
                    if scrollsSettings { settingsEntry }
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
            .refreshable {
                await store.refreshVisible()
                await store.refreshInbox()
            }
            if !scrollsSettings {
                settingsEntry
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
            }
        }
        .background(XTheme.background)
        .tint(XTheme.accent)
        .onAppear { accessibilityFocus = .heading }
    }

    private var resultScopes: some View {
        VStack(alignment: .leading, spacing: 8) {
            if dynamicTypeSize.isAccessibilitySize {
                scopePicker.pickerStyle(.menu)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            } else {
                scopePicker.pickerStyle(.segmented)
            }
            if scope == .running {
                Text("显示已载入的进行中任务。")
                    .font(.caption).foregroundStyle(XTheme.secondary)
            }
        }
        .onChange(of: scope) { _, value in
            if value == .unread { Task { await store.refreshInbox() } }
        }
    }

    private var scopePicker: some View {
        Picker("记录范围", selection: $scope) {
            Text("全部").tag(HistoryScope.all)
            Text("进行中").tag(HistoryScope.running)
            Text("待查看 \(store.unreadCount)").tag(HistoryScope.unread)
        }.accessibilityLabel("记录范围")
    }

    private var inboxContent: some View {
        LazyVStack(alignment: .leading, spacing: 8) {
            if let note = store.inboxNote {
                Text(note).font(.subheadline).foregroundStyle(XTheme.secondary)
                    .fixedSize(horizontal: false, vertical: true).padding(14)
                Button("重新读取结果") { Task { await store.refreshInbox() } }
                    .buttonStyle(XPressButtonStyle()).frame(minHeight: 44)
                    .padding(.horizontal, 14)
            }
            if filteredResults.isEmpty {
                if store.isRefreshingInbox && store.inboxItems.isEmpty {
                    ProgressView("正在读取结果…").tint(XTheme.accent).padding(14)
                } else if store.inboxNote == nil {
                    Text(store.hasConnection ? (search.isEmpty && projectFilter == nil ? "结果都已查看。新的结果会出现在这里。" : "已载入结果中没有匹配项，可调整筛选。") : "设置连接后查看新结果。")
                        .font(.subheadline).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true).padding(14)
                }
            }
            ForEach(filteredResults) { item in
                Button {
                    searchFocused = false
                    Task { await store.openInboxItem(item) }
                } label: {
                    VStack(alignment: .leading, spacing: 9) {
                        HStack(alignment: .top, spacing: 8) {
                            Circle().fill(XTheme.accent).frame(width: 6, height: 6).padding(.top, 8)
                                .accessibilityHidden(true)
                            Text(item.title).font(.body.weight(.medium)).foregroundStyle(XTheme.text)
                                .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 8) {
                                Text(store.projectName(item.project_id, nodeID: item.node_id)).font(.caption).foregroundStyle(XTheme.secondary)
                                Spacer(minLength: 2)
                                XStatusPill(status: item.status)
                            }
                            VStack(alignment: .leading, spacing: 7) {
                                Text(store.projectName(item.project_id, nodeID: item.node_id)).font(.caption).foregroundStyle(XTheme.secondary)
                                XStatusPill(status: item.status)
                            }
                        }
                        Text(relativeTime(item.created_at) ?? "时间未确认")
                            .font(.caption).foregroundStyle(XTheme.secondary)
                    }
                    .padding(14).frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 16))
                }
                .buttonStyle(XPressButtonStyle())
                .accessibilityLabel("待查看结果：\(item.title)，\(store.projectName(item.project_id, nodeID: item.node_id))")
                .accessibilityHint("打开并查看这一轮结果")
            }
            if store.inboxNextCursor != nil {
                Button {
                    Task { await store.loadMoreInbox() }
                } label: {
                    Text(store.isLoadingMoreInbox ? "正在读取…" : "读取更多待查看结果")
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(XPressButtonStyle())
                .disabled(store.isLoadingMoreInbox || store.isRefreshingInbox)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            XVISMark(size: 28)
            Text("对话记录")
                .font(.headline)
                .foregroundStyle(XTheme.text)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
                .accessibilityFocused($accessibilityFocus, equals: .heading)
            Spacer(minLength: 0)
            if searchFocused {
                DismissKeyboardButton { searchFocused = false }
            }
            Button(action: close) { Image(systemName: "xmark").font(.body.weight(.medium)) }
                .buttonStyle(XIconButtonStyle())
                .accessibilityLabel("关闭历史记录")
        }
        .padding(.leading, 20)
        .padding(.trailing, 12)
        .padding(.top, 8)
        .padding(.bottom, 8)
    }

    private var searchField: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass").foregroundStyle(XTheme.secondary).accessibilityHidden(true)
            TextField("搜索已载入的任务", text: $search)
                .font(.body)
                .foregroundStyle(XTheme.text)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .focused($searchFocused)
                .accessibilityLabel("搜索已载入的任务")
                .accessibilityHint("仅搜索手机已载入的任务标题、指令和工作区名称")
                .accessibilityFocused($accessibilityFocus, equals: .search)
            if !search.isEmpty {
                Button { search = "" } label: { Image(systemName: "xmark.circle.fill").font(.body) }
                    .buttonStyle(XIconButtonStyle())
                    .accessibilityLabel("清除搜索")
            }
        }
        .frame(minHeight: 44)
        .padding(.leading, 14)
        .padding(.trailing, search.isEmpty ? 14 : 4)
        .padding(.vertical, 6)
        .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(XTheme.border, lineWidth: 0.75) }
    }

    private var workspaceFilter: some View {
        Menu {
            Button { projectFilter = nil } label: {
                Label("全部工作区", systemImage: projectFilter == nil ? "checkmark" : "folder")
            }
            ForEach(projectChoices, id: \.self) { projectID in
                Button { projectFilter = projectID } label: {
                    Label(store.projectName(projectID), systemImage: projectFilter == projectID ? "checkmark" : "folder")
                }
            }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "line.3.horizontal.decrease").accessibilityHidden(true)
                Text(filterLabel).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(.caption.weight(.semibold)).accessibilityHidden(true)
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(XTheme.accent)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .accessibilityLabel("筛选工作区：\(filterLabel)")
        .accessibilityHint("选择工作区或显示全部已载入任务")
    }

    private var historyContent: some View {
        LazyVStack(alignment: .leading, spacing: 8) {
            if filteredTasks.isEmpty {
                if store.isRefreshing && store.tasks.isEmpty {
                    ProgressView("正在读取历史…")
                        .tint(XTheme.accent)
                        .font(.subheadline)
                        .foregroundStyle(XTheme.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
                } else {
                    Text(emptyMessage)
                        .font(.subheadline)
                        .foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(14)
                }
            }
            ForEach(sections) { section in
                Text(section.period.label)
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(XTheme.secondary)
                    .padding(.horizontal, 14)
                    .padding(.top, 16)
                    .padding(.bottom, 2)
                    .accessibilityAddTraits(.isHeader)
                ForEach(section.tasks) { task in
                    taskRow(task)
                }
            }
            if store.nextCursor != nil {
                Button { Task { await store.loadMoreTasks() } } label: {
                    HStack(spacing: 8) {
                        if store.isLoadingMore { ProgressView().tint(XTheme.accent) }
                        Text(store.isLoadingMore ? "正在读取…" : "读取更早的任务")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(XTheme.accent)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .padding(.vertical, 6)
                    .padding(.horizontal, 12)
                    .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
                .buttonStyle(XPressButtonStyle())
                .disabled(store.isLoadingMore || store.isRefreshing)
                .padding(.top, 12)
            }
        }
    }

    private var emptyMessage: String {
        if !search.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || projectFilter != nil {
            return "已载入记录中没有匹配的任务，可调整筛选或读取更早内容。"
        }
        if scope == .running && store.hasConnection {
            return "已载入的记录中，暂时没有进行中的任务。"
        }
        return store.hasConnection ? "还没有任务记录，从新对话开始吧。" : "设置连接后查看任务记录。"
    }

    private func taskRow(_ task: TaskView) -> some View {
        let selected = task.id == selectedTaskID
        let time = relativeTime(task.created_at) ?? "时间未确认"
        let round = task.is_followup_of == nil ? "首轮任务" : "续接轮次"
        return Button {
            searchFocused = false
            selectTask(task.id)
        } label: {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 8) {
                    Text(taskTitle(task))
                        .font(.body.weight(selected ? .semibold : .regular))
                        .foregroundStyle(XTheme.text)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if selected {
                        Image(systemName: "checkmark")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(XTheme.accent)
                            .padding(.top, 4)
                            .accessibilityHidden(true)
                    } else if store.inboxItems.contains(where: { $0.task_id == task.id }) {
                        Circle().fill(XTheme.accent).frame(width: 7, height: 7).padding(.top, 6)
                            .accessibilityLabel("结果待查看")
                    }
                }
                if dynamicTypeSize.isAccessibilitySize {
                    verticalMetadata(task)
                } else {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 8) {
                            workspaceName(task).fixedSize(horizontal: true, vertical: false)
                            Spacer(minLength: 4)
                            XStatusPill(status: task.status).fixedSize(horizontal: true, vertical: false)
                        }
                        verticalMetadata(task)
                    }
                }
                Text("\(round) · \(time)")
                    .font(.caption)
                    .foregroundStyle(XTheme.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 14)
            .background(selected ? XTheme.soft : Color.clear, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(alignment: .leading) {
                if selected {
                    Capsule().fill(XTheme.accent).frame(width: 3).padding(.vertical, 14).allowsHitTesting(false)
                }
            }
        }
        .buttonStyle(XPressButtonStyle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(taskTitle(task))，\(store.projectName(task.project_id, nodeID: task.node_id))，\(task.statusLabel)，\(round)，\(time)")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityHint("打开这一轮任务")
    }

    private func workspaceName(_ task: TaskView) -> some View {
        Label(store.projectName(task.project_id, nodeID: task.node_id), systemImage: "folder")
            .font(.caption)
            .foregroundStyle(XTheme.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func verticalMetadata(_ task: TaskView) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            workspaceName(task)
            XStatusPill(status: task.status)
        }
    }

    private var settingsEntry: some View {
        VStack(spacing: 16) {
            Rectangle().fill(XTheme.border).frame(height: 0.75).accessibilityHidden(true)
            Button {
                searchFocused = false
                showSettings()
            } label: {
                HStack(alignment: .center, spacing: 12) {
                    Image(systemName: "slider.horizontal.3")
                        .font(.title3)
                        .foregroundStyle(XTheme.accent)
                        .frame(width: 44, height: 44)
                        .background(XTheme.soft, in: Circle())
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("设置").font(.body.weight(.medium)).foregroundStyle(XTheme.text)
                        Text(connectionSummary).font(.caption).foregroundStyle(XTheme.secondary)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.right").font(.caption.weight(.medium)).foregroundStyle(XTheme.secondary)
                        .accessibilityHidden(true)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            .buttonStyle(XPressButtonStyle())
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("设置，\(connectionSummary)")
            .accessibilityHint("打开记忆、人格、外观和连接设置")
        }
        .padding(.top, 12)
    }
}

private enum HistoryScope: Hashable { case all, running, unread }

private enum HistoryPeriod: String, CaseIterable {
    case today, yesterday, pastWeek, earlier
    var label: String {
        switch self {
        case .today: return "今天"
        case .yesterday: return "昨天"
        case .pastWeek: return "过去 7 天"
        case .earlier: return "更早"
        }
    }
}

private struct HistorySection: Identifiable {
    let period: HistoryPeriod
    let tasks: [TaskView]
    var id: HistoryPeriod { period }
}
