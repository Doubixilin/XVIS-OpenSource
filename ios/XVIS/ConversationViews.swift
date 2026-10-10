import SwiftUI
import UIKit
import SafariServices

struct ConversationView: View {
    let taskID: String
    let openTask: (String) -> Void
    let retryTask: (TaskView) -> Void
    let dismissKeyboard: () -> Void
    @EnvironmentObject private var store: AppStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hasPositioned = false
    @State private var followsLatest = true
    @State private var dragging = false
    @State private var legacyTowardsLatest = false
    @State private var showNewResults = false
    @State private var bottomDistance: CGFloat = .greatestFiniteMagnitude
    @State private var imageViewer: XImageViewerContent?
    @State private var webpage: ReadingWebpage?
    @State private var bookmarkSave: Task<Void, Never>?
    private let bottomID = "xvis-conversation-bottom"

    private var taskIDs: [String] { store.conversationTaskIDs(for: taskID) }
    private var latestRevision: ConversationRevision? {
        guard let id = taskIDs.last else { return nil }
        let cache = store.state.caches[id] ?? TaskCache()
        return ConversationRevision(id: id, status: cache.snapshot?.status, after: cache.after, textSize: cache.finalText.utf8.count, imageCount: cache.snapshot?.images?.count ?? 0)
    }

    var body: some View {
        GeometryReader { viewport in
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 32) {
                        if let note = store.conversationNote {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(note).font(.footnote).foregroundStyle(XTheme.secondary)
                                Button("重新读取") { Task { await store.loadConversation(containing: taskID) } }
                                    .font(.footnote.weight(.medium)).tint(XTheme.accent)
                                    .frame(minHeight: 44).buttonStyle(XPressButtonStyle())
                            }
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 16))
                        }
                        ForEach(taskIDs, id: \.self) { id in
                            ForEach((store.state.caches[id]?.snapshot?.interactions ?? []).filter { $0.status == "pending" || $0.status == "submitted" }) { item in
                                InteractionCard(item: item).id("interaction-" + item.id)
                            }
                            ConversationRoundView(
                                taskID: id,
                                isLatest: id == taskIDs.last,
                                allowFeedback: hasPositioned,
                                openTask: openTask,
                                retryTask: retryTask,
                                openImage: presentImage
                            )
                            .id(id)
                        }
                        Color.clear.frame(height: 1).id(bottomID)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 22)
                    .padding(.top, 12)
                    .padding(.bottom, 26)
                    .background {
                        // The stack exists even when its bottom item is outside the lazy viewport.
                        GeometryReader { content in
                            Color.clear.preference(key: ConversationBottomKey.self, value: content.frame(in: .named("xvis-conversation-scroll")).maxY)
                        }
                    }
                }
                .coordinateSpace(name: "xvis-conversation-scroll")
                .scrollDismissesKeyboard(.interactively)
                .scrollBounceBehavior(.basedOnSize)
                .modifier(ConversationLegacyDragObserver(changed: legacyDragChanged, ended: { dragging = false }))
                .modifier(ConversationScrollObserver(update: updateBottomDistance, phaseChanged: userScrollPhaseChanged))
                .onPreferenceChange(ConversationBottomKey.self) { position in
                    if #available(iOS 18.0, *) { }
                    else { updateBottomDistance(position - viewport.size.height) }
                }
                .onPreferenceChange(ReadingBlockKey.self) { positions in
                    guard hasPositioned else { return }
                    let visible = positions.filter { $0.value.maxY > 0 && $0.value.minY < viewport.size.height }
                    let anchor = visible.first(where: { $0.value.minY <= 0 && $0.value.maxY > 0 })?.key
                        ?? visible.min(by: { $0.value.minY < $1.value.minY })?.key
                    guard let anchor else { return }
                    bookmarkSave?.cancel()
                    bookmarkSave = Task { @MainActor in
                        do { try await Task.sleep(for: .milliseconds(400)) } catch { return }
                        guard hasPositioned, !Task.isCancelled else { return }
                        store.saveReadingBookmark(anchor, conversationID: taskID)
                    }
                }
                .background {
                    XTheme.background
                        .contentShape(Rectangle())
                        .onTapGesture { dismissKeyboard() }
                }
                .overlay(alignment: .top) {
                    if store.conversationLoading {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).tint(XTheme.accent)
                            Text("正在补齐对话记录…").font(.caption).foregroundStyle(XTheme.secondary)
                        }
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(XTheme.surface, in: Capsule())
                        .overlay { Capsule().strokeBorder(XTheme.border, lineWidth: 0.75) }
                        .padding(.top, 8)
                        .allowsHitTesting(false)
                    }
                }
                .overlay(alignment: .bottom) {
                    if showNewResults {
                        Button {
                            dragging = false
                            followsLatest = true
                            showNewResults = false
                            scrollToLatest(proxy, animated: true)
                        } label: {
                            Label("查看新结果", systemImage: "arrow.down")
                                .font(.subheadline.weight(.medium))
                                .padding(.horizontal, 16).frame(minHeight: 44)
                                .foregroundStyle(XTheme.onAccent)
                                .background(XTheme.accent, in: Capsule())
                        }
                        .buttonStyle(XPressButtonStyle())
                        .padding(.bottom, 10)
                        .transition(.opacity)
                    }
                }
                .refreshable {
                    await store.loadConversation(containing: taskID)
                    await store.confirmInboxResultDisplayed(taskID: taskID)
                }
                .task(id: taskID) {
                    hasPositioned = false
                    await store.loadConversation(containing: taskID)
                    guard !Task.isCancelled else { return }
                    await Task.yield()
                    let selectedCache = store.state.caches[taskID] ?? TaskCache()
                    if let focus = store.requestedInteractionID, selectedCache.snapshot?.interactions?.contains(where: { $0.id == focus }) == true {
                        followsLatest = false
                        proxy.scrollTo("interaction-" + focus, anchor: .top)
                        store.requestedInteractionID = nil
                    } else if let pending = selectedCache.snapshot?.interactions?.first(where: { $0.status == "pending" || $0.status == "submitted" }) {
                        followsLatest = false
                        proxy.scrollTo("interaction-" + pending.id, anchor: .top)
                    } else if let bookmark = store.state.readingBookmarks[taskID], taskIDs.contains(where: { bookmark.hasPrefix($0 + "-answer-") }) {
                        followsLatest = false
                        proxy.scrollTo(bookmark, anchor: .top)
                    } else if taskID == taskIDs.last, selectedCache.snapshot?.terminal == false, selectedCache.finalText.isEmpty {
                        followsLatest = true
                        scrollToLatest(proxy, animated: false)
                    } else {
                        // Open a long completed round at its instruction, rather than its answer's end.
                        followsLatest = false
                        proxy.scrollTo(taskID, anchor: .top)
                    }
                    hasPositioned = true
                    if bottomDistance <= 96 { followsLatest = true }
                    await store.confirmInboxResultDisplayed(taskID: taskID)
                }
                .onChange(of: latestRevision) { old, new in
                    guard hasPositioned, let old, let new, old != new else { return }
                    if followsLatest && !dragging {
                        // Wait for the new layout, without animating already-readable Markdown.
                        Task { @MainActor in
                            await Task.yield()
                            guard followsLatest, !dragging else { return }
                            scrollToLatest(proxy, animated: false)
                        }
                    } else { showNewResults = true }
                }
                .onChange(of: viewport.size.height) { _, _ in
                    if followsLatest && !dragging { scrollToLatest(proxy, animated: false) }
                }
            }
        }
        .foregroundStyle(XTheme.text)
        .tint(XTheme.accent)
        // Keep one presenter outside the lazy rounds and their nested image scroll views.
        .fullScreenCover(item: $imageViewer) { XImageViewer(content: $0) }
        .sheet(item: $webpage) { ReadingBrowser(url: $0.url).ignoresSafeArea() }
        .environment(\.openURL, OpenURLAction { url in
            guard ["https", "http"].contains(url.scheme?.lowercased() ?? ""), url.host != nil,
                  url.user == nil, url.password == nil else { return .systemAction }
            dismissKeyboard(); webpage = ReadingWebpage(url: url)
            return .handled
        })
        .onDisappear { bookmarkSave?.cancel() }
        .onChange(of: taskID) { _, _ in imageViewer = nil }
        .onChange(of: store.state.caches[taskID]?.snapshot?.status) { _, _ in
            guard hasPositioned else { return }
            Task { await store.confirmInboxResultDisplayed(taskID: taskID) }
        }
    }

    private func presentImage(_ content: XImageViewerContent) {
        dismissKeyboard()
        imageViewer = content
    }

    private func updateBottomDistance(_ distance: CGFloat) {
        bottomDistance = max(0, distance)
        guard hasPositioned else { return }
        if #available(iOS 18.0, *) {
            // Geometry also changes when content grows or scrollTo moves the view.
            // Those updates must not overwrite the user's decision to read earlier content.
            if dragging { followsLatest = false }
        } else if legacyTowardsLatest, bottomDistance <= 96 {
            // iOS 17 only resumes after a deliberate gesture towards the end reaches it.
            followsLatest = true
            showNewResults = false
        }
    }

    private func userScrollPhaseChanged(_ active: Bool, _ ended: Bool, _ distance: CGFloat) {
        bottomDistance = max(0, distance)
        guard hasPositioned else { return }
        if active {
            dragging = true
            followsLatest = false
        } else if ended {
            dragging = false
            // Use the final native scroll geometry, including deceleration, not a stale drag-end sample.
            followsLatest = bottomDistance <= 96
            if followsLatest { showNewResults = false }
        }
    }

    private func legacyDragChanged(_ horizontal: CGFloat, _ vertical: CGFloat) {
        guard abs(vertical) > abs(horizontal), abs(vertical) > 8 else { return }
        dragging = true
        followsLatest = false
        legacyTowardsLatest = vertical < 0
        if legacyTowardsLatest, bottomDistance <= 96 {
            followsLatest = true
            showNewResults = false
        }
    }

    private func scrollToLatest(_ proxy: ScrollViewProxy, animated: Bool) {
        if let latest = taskIDs.last,
           let pending = store.state.caches[latest]?.snapshot?.interactions?.first(where: { $0.status == "pending" }) {
            proxy.scrollTo("interaction-" + pending.id, anchor: .top)
            followsLatest = false
            return
        }
        if animated && !reduceMotion {
            withAnimation(.smooth(duration: 0.28)) { proxy.scrollTo(bottomID, anchor: .bottom) }
        } else { proxy.scrollTo(bottomID, anchor: .bottom) }
    }
}

private struct ConversationScrollObserver: ViewModifier {
    let update: (CGFloat) -> Void
    let phaseChanged: (Bool, Bool, CGFloat) -> Void

    @ViewBuilder func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollGeometryChange(for: CGFloat.self) { geometry in
                max(0, geometry.contentSize.height - geometry.visibleRect.maxY)
            } action: { _, distance in update(distance) }
            .onScrollPhaseChange { old, new, context in
                let active = new == .tracking || new == .interacting || new == .decelerating
                let wasActive = old == .tracking || old == .interacting || old == .decelerating
                let distance = max(0, context.geometry.contentSize.height - context.geometry.visibleRect.maxY)
                phaseChanged(active, wasActive && new == .idle, distance)
            }
        } else { content }
    }
}

private struct ConversationLegacyDragObserver: ViewModifier {
    let changed: (CGFloat, CGFloat) -> Void
    let ended: () -> Void

    @ViewBuilder func body(content: Content) -> some View {
        if #available(iOS 18.0, *) { content }
        else {
            content.simultaneousGesture(DragGesture(minimumDistance: 8)
                .onChanged { value in changed(value.translation.width, value.translation.height) }
                .onEnded { _ in ended() })
        }
    }
}

private struct ConversationRevision: Equatable {
    let id: String
    let status: String?
    let after: Int
    let textSize: Int
    let imageCount: Int
}

private struct ConversationBottomKey: PreferenceKey {
    static var defaultValue: CGFloat = .greatestFiniteMagnitude
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = min(value, nextValue()) }
}

private struct ConversationRoundView: View {
    let taskID: String
    let isLatest: Bool
    let allowFeedback: Bool
    let openTask: (String) -> Void
    let retryTask: (TaskView) -> Void
    let openImage: (XImageViewerContent) -> Void
    @EnvironmentObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var promptExpanded = false
    @State private var cancelling = false
    @State private var showCancel = false
    @State private var showProjectSnapshot = false
    @State private var projectContextRoute: ProjectContextRoute?
    private var cache: TaskCache { store.state.caches[taskID] ?? TaskCache() }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if let task = cache.snapshot {
                let inputImages = (task.images ?? []).filter { $0.direction == "input" }
                let outputImages = (task.images ?? []).filter { $0.direction == "output" }
                if let prompt = task.prompt {
                    VStack(alignment: .trailing, spacing: 6) {
                        HStack {
                            Spacer(minLength: 38)
                            VStack(alignment: .leading, spacing: 8) {
                                Text(prompt).font(.body).textSelection(.enabled).lineSpacing(5)
                                    .lineLimit(promptExpanded || prompt.count <= 600 ? nil : 8)
                                    .fixedSize(horizontal: false, vertical: true)
                                TaskImages(images: inputImages, taskID: taskID, openImage: openImage)
                                if prompt.count > 600 {
                                    Button(promptExpanded ? "收起原指令" : "展开完整指令") { promptExpanded.toggle() }
                                        .font(.footnote.weight(.medium)).foregroundStyle(XTheme.accent)
                                        .frame(minHeight: 44).buttonStyle(XPressButtonStyle())
                                }
                            }
                            .padding(.horizontal, 17).padding(.vertical, 13)
                            .background(XTheme.soft, in: RoundedRectangle(cornerRadius: 21, style: .continuous))
                        }
                        HStack(spacing: 6) {
                            if let time = relativeTime(task.created_at) { Text(time).font(.caption).foregroundStyle(XTheme.secondary) }
                            ClipboardButton(text: prompt, accessibility: "复制完整指令")
                        }
                    }
                }
                HStack(spacing: 8) {
                    XVISMark(size: 25)
                    Text(task.assistant_name ?? "XVIS").font(.subheadline.weight(.semibold))
                    Spacer()
                    if !isLatest {
                        Text("较早一轮").font(.caption).foregroundStyle(XTheme.secondary)
                    }
                }
                ForEach((task.interactions ?? []).filter { $0.isValid && $0.status != "pending" && $0.status != "submitted" }) { item in
                    InteractionCard(item: item).id("interaction-" + item.id)
                }
                TaskExecutionCard(task: task, steps: cache.steps, cancelling: cancelling, stop: { showCancel = true })
                if let control = task.control, control.isValid {
                    ContinuousControlCard(task: task, control: control)
                }
                if let reason = task.waiting_reason, !reason.isEmpty, task.status == "queued" {
                    Label(reason, systemImage: "clock.badge.exclamationmark")
                        .font(.footnote).foregroundStyle(XTheme.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { projectActions(task) }
                    VStack(alignment: .leading, spacing: 2) { projectActions(task) }
                }.font(.footnote).foregroundStyle(XTheme.accent)
                Group {
                    if !cache.finalText.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            MarkdownAnswerView(source: cache.finalText, identity: taskID)
                            HStack(spacing: 8) {
                                ClipboardButton(text: cache.finalText, accessibility: "复制回答")
                                ShareLink(item: cache.finalText) {
                                    Image(systemName: "square.and.arrow.up").font(.body).frame(width: 44, height: 44)
                                }
                                .buttonStyle(XIconButtonStyle()).accessibilityLabel("分享回答")
                                Spacer(minLength: 0)
                            }
                        }
                        .transition(.opacity)
                    } else if task.status == "done" && outputImages.isEmpty && (task.output_files ?? []).isEmpty {
                        Text("本轮已完成，没有文本回答。")
                            .font(.body).foregroundStyle(XTheme.secondary)
                    }
                }
                // Only the first live text arrival fades in; chunks, history and polling stay still.
                .animation(reduceMotion || !allowFeedback || !isLatest ? nil : .easeOut(duration: 0.18), value: cache.finalText.isEmpty)
                TaskImages(images: outputImages, taskID: taskID, openImage: openImage)
                TaskFilesCard(task: task)
                if let note = cache.steps.last(where: { $0.eventType == "error" && $0.text.contains("图片") }) {
                    Label(note.text, systemImage: "photo.badge.exclamationmark")
                        .font(.footnote).foregroundStyle(XTheme.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = task.error {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("执行提示", systemImage: "exclamationmark.circle")
                            .font(.subheadline.weight(.medium)).foregroundStyle(XTheme.error)
                        Text(error.message).font(.subheadline).lineSpacing(4).textSelection(.enabled)
                    }
                    .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                    .background(XTheme.errorBackground, in: RoundedRectangle(cornerRadius: 16))
                }
                if ["failed", "timed_out"].contains(task.status), task.prompt != nil {
                    Button { retryTask(task) } label: {
                        Label("新建任务重试", systemImage: "arrow.clockwise")
                            .font(.subheadline.weight(.medium))
                            .padding(.horizontal, 14).frame(minHeight: 44)
                            .background(XTheme.soft, in: RoundedRectangle(cornerRadius: 13))
                    }
                    .foregroundStyle(XTheme.accent).buttonStyle(XPressButtonStyle())
                    .disabled(store.isSending || store.state.pending != nil)
                }
                if task.status == "interrupted" {
                    Text("执行已中断，请在执行设备上检查。已经完成的修改会保留。")
                        .font(.footnote).foregroundStyle(XTheme.warning)
                } else if task.status == "cancelled" {
                    Text("已确认取消，执行设备已完成的修改会保留。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                }
                if cache.finalTruncated {
                    Text("回答较长，手机显示已截断。完整结果可在执行设备上查看。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                } else if cache.finalStarted && !cache.finalComplete {
                    Text("正在补齐完整回答…").font(.footnote).foregroundStyle(XTheme.secondary)
                }
                if !task.terminal, store.nodes.first(where: { $0.id == task.node_id })?.online == false {
                    Label(task.status == "queued" ? "等待执行设备上线" : "执行设备暂时离线，正在等待同步", systemImage: "wifi.slash")
                        .font(.footnote).foregroundStyle(XTheme.warning)
                }
                if isLatest, let following = task.followup_task_id, !store.conversationTaskIDs(for: taskID).contains(following) {
                    Button { openTask(following) } label: { Label("查看后续内容", systemImage: "arrow.right") }
                        .font(.subheadline).frame(minHeight: 44).buttonStyle(XPressButtonStyle())
                }
            } else {
                ProgressView("正在读取本轮…").tint(XTheme.accent)
                    .frame(maxWidth: .infinity).padding(.vertical, 24)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: cache.snapshot?.status) { old, new in
            guard allowFeedback, isLatest, scenePhase == .active,
                  let old, let new, old != new,
                  !["done", "failed", "cancelled", "timed_out", "interrupted"].contains(old) else { return }
            // Text delivery is independent of execution success. Only real terminal states provide feedback.
            if new == "done" { Haptics.completed() }
            else if ["failed", "timed_out", "interrupted"].contains(new) { Haptics.failed() }
        }
        .confirmationDialog("停止这项任务？执行设备已完成的修改会保留。", isPresented: $showCancel, titleVisibility: .visible) {
            Button("停止任务", role: .destructive) {
                cancelling = true
                Task { await store.cancelTask(taskID); cancelling = false }
            }
            Button("继续执行", role: .cancel) { }
        }
        .sheet(isPresented: $showProjectSnapshot) { ProjectSnapshotView(context: cache.snapshot?.project_context, openTask: openTask) }
        .sheet(item: $projectContextRoute) { route in
            ProjectContextView(route: route, openTask: { id in projectContextRoute = nil; openTask(id) })
        }
    }
    @ViewBuilder private func projectActions(_ task: TaskView) -> some View {
        Button(task.project_context_revision.map { "本轮背景 · v\($0)" } ?? "本轮项目背景") { showProjectSnapshot = true }
            .frame(minHeight: 44).accessibilityLabel("查看本轮实际带入的项目背景")
        Button("更新项目背景") {
            projectContextRoute = ProjectContextRoute(nodeID: task.node_id, projectID: task.project_id,
                                                     sourceTaskID: task.terminal ? task.id : nil)
        }.frame(minHeight: 44).accessibilityHint("编辑后只用于新对话，不改变当前会话背景")
    }
}

private struct TaskExecutionCard: View {
    let task: TaskView
    let steps: [StepEntry]
    let cancelling: Bool
    let stop: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expanded = false
    private var waiting: Bool { (task.interactions ?? []).contains { $0.status == "pending" || $0.status == "submitted" } }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) { XStatusPill(status: waiting ? "awaiting_input" : task.status); Spacer(minLength: 8); elapsed }
                VStack(alignment: .leading, spacing: 8) { XStatusPill(status: waiting ? "awaiting_input" : task.status); elapsed }
            }
            if !task.terminal {
                HStack(alignment: .top, spacing: 8) {
                    if ["assigned", "running"].contains(task.status) && !reduceMotion && !waiting {
                        ProgressView().controlSize(.mini).tint(XTheme.accent).accessibilityHidden(true)
                    }
                    Text(activityText).font(.footnote).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !expanded, !task.terminal, !steps.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(steps.suffix(3))) { step in
                        ExecutionEventRow(step: step, detailed: false, isLast: step.id == steps.last?.id)
                    }
                }
            }
            if expanded {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(steps) { step in
                        ExecutionEventRow(step: step, detailed: true, isLast: step.id == steps.last?.id)
                    }
                    if steps.count == 300 {
                        Text("手机保留最近 300 条事件，完整记录可在执行设备上查看。")
                            .font(.caption).foregroundStyle(XTheme.secondary).padding(.top, 8)
                    }
                }
            }
            if !steps.isEmpty || !task.terminal {
                HStack(spacing: 12) {
                    if !steps.isEmpty {
                        Button {
                            if reduceMotion { expanded.toggle() }
                            else { withAnimation(.smooth(duration: 0.26)) { expanded.toggle() } }
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).rotationEffect(.degrees(expanded ? 90 : 0))
                                Text(expanded ? "收起过程" : "执行过程 · \(steps.count) 条")
                            }
                            .font(.footnote).frame(minHeight: 44)
                        }
                        .foregroundStyle(XTheme.secondary).buttonStyle(XPressButtonStyle())
                        .accessibilityLabel(expanded ? "收起执行过程" : "展开执行过程，\(steps.count) 条事件")
                    }
                    Spacer(minLength: 0)
                    if !task.terminal {
                        Button(cancelling ? "正在请求…" : task.status == "cancel_requested" ? "等待确认" : "停止", role: .destructive, action: stop)
                            .font(.footnote.weight(.medium)).frame(minWidth: 44, minHeight: 44)
                            .foregroundStyle(XTheme.error).buttonStyle(XPressButtonStyle())
                            .disabled(cancelling || task.status == "cancel_requested")
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(XTheme.border, lineWidth: 0.75) }
    }

    private var activityText: String {
        if waiting { return "执行设备正在等待这次交互完成；退出 App 后也会保留。" }
        switch task.status {
        case "queued": return "任务已保存，等待执行设备接手。"
        case "assigned": return "执行设备正在准备本轮任务。"
        case "running":
            if let last = steps.last(where: { ["tool_call", "tool_result", "text"].contains($0.eventType ?? "") }) {
                if last.eventType == "tool_call" { return last.text }
                if last.eventType == "text" { return "正在整理回答，正文将持续更新。" }
                if last.status == "error" { return "上一步遇到限制，正在等待后续处理。" }
                return "上一步已返回，正在继续处理。"
            }
            return "正在处理本轮任务，可以补充方向或安排下一步。"
        case "cancel_requested": return "停止请求已收到，正在等待执行设备确认。"
        default: return "正在核对任务状态。"
        }
    }

    @ViewBuilder private var elapsed: some View {
        if task.terminal {
            if let start = task.started_at.flatMap(taskDate), let finish = task.finished_at.flatMap(taskDate) {
                Text("用时 \(duration(finish.timeIntervalSince(start)))")
                    .font(.caption.monospacedDigit()).foregroundStyle(XTheme.secondary)
            }
        } else {
            TimelineView(.periodic(from: .now, by: 1)) { timeline in
                if let start = task.started_at.flatMap(taskDate) {
                    Text("已运行 \(duration(timeline.date.timeIntervalSince(start)))")
                        .font(.caption.monospacedDigit()).foregroundStyle(XTheme.secondary)
                } else if task.status == "queued", let created = taskDate(task.created_at) {
                    Text("已等待 \(duration(timeline.date.timeIntervalSince(created)))")
                        .font(.caption.monospacedDigit()).foregroundStyle(XTheme.secondary)
                }
            }
        }
    }

    private func duration(_ value: TimeInterval) -> String {
        let seconds = max(0, Int(value))
        if seconds >= 3600 { return "\(seconds / 3600) 小时 \((seconds % 3600) / 60) 分" }
        if seconds >= 60 { return "\(seconds / 60) 分 \(seconds % 60) 秒" }
        return "\(seconds) 秒"
    }
}

private struct ExecutionEventRow: View {
    let step: StepEntry
    let detailed: Bool
    let isLast: Bool
    private var isError: Bool { step.eventType == "error" || step.status == "error" }
    private var icon: String {
        if isError { return "exclamationmark.circle.fill" }
        switch step.eventType {
        case "tool_call": return "wrench.and.screwdriver"
        case "tool_result": return "arrow.turn.down.right"
        case "text": return "text.alignleft"
        case "status": return step.phase == "turn_start" ? "play.circle" : "circle.fill"
        default: return "circle.fill"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(spacing: 4) {
                Image(systemName: icon).font(.system(size: icon == "circle.fill" ? 5 : 12))
                    .frame(width: 16, height: 19)
                    .foregroundStyle(isError ? XTheme.error : XTheme.accent)
                    .accessibilityHidden(true)
                if !isLast { Rectangle().fill(XTheme.border).frame(width: 1).frame(maxHeight: .infinity).accessibilityHidden(true) }
            }
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(step.title).font(detailed ? .subheadline.weight(.medium) : .caption.weight(.medium))
                        .foregroundStyle(isError ? XTheme.error : XTheme.text)
                    if let date = step.createdAt.flatMap(taskDate) {
                        Spacer(minLength: 0)
                        Text(date.formatted(.dateTime.hour().minute())).font(.caption2.monospacedDigit()).foregroundStyle(XTheme.secondary)
                    }
                }
                if !step.text.isEmpty {
                    Text(detailed ? step.text : step.text.replacingOccurrences(of: "\n", with: " "))
                        .font(detailed ? .footnote : .caption).foregroundStyle(XTheme.secondary)
                        .lineLimit(detailed ? nil : 1).textSelection(.enabled)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, isLast ? 0 : 12)
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
    }
}

private struct ClipboardButton: View {
    let text: String
    var label: String? = nil
    let accessibility: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var copied = false

    var body: some View {
        Button {
            UIPasteboard.general.string = text
            copied = true
        } label: {
            HStack(spacing: 5) {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
                if let label { Text(copied ? "已复制" : label) }
            }
            .font(.footnote).frame(minWidth: 44, minHeight: 44)
            .foregroundStyle(copied ? XTheme.accent : XTheme.secondary)
        }
        .buttonStyle(XPressButtonStyle())
        .accessibilityLabel(copied ? "已复制" : accessibility)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.16), value: copied)
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(for: .seconds(1.5))
            guard !Task.isCancelled else { return }
            copied = false
        }
    }
}

private struct ReadingBlockKey: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, newest in newest })
    }
}
private struct ReadingWebpage: Identifiable { let url: URL; var id: String { url.absoluteString } }
private struct ReadingBrowser: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> SFSafariViewController { SFSafariViewController(url: url) }
    func updateUIViewController(_ controller: SFSafariViewController, context: Context) { }
}

/// Only a blank-line boundary outside a code fence can freeze the preceding prefix.
/// This keeps incomplete tables/fences in the reparsed tail and preserves parser parity.
final class MarkdownIncrementalCache {
    private var prefix = ""
    private var stable: [MarkdownAnswerBlock] = []
    private var lastSource = ""
    private var lastBlocks: [MarkdownAnswerBlock] = []
    private(set) var reusedPrefixCount = 0
    func blocks(for source: String) -> [MarkdownAnswerBlock] {
        if source == lastSource { return lastBlocks }
        let cut = Self.boundary(source)
        let newPrefix = String(source[..<cut])
        if !source.hasPrefix(prefix) || newPrefix.count < prefix.count { prefix = ""; stable = [] }
        if !prefix.isEmpty { reusedPrefixCount += 1 }
        if newPrefix.count > prefix.count {
            let suffix = String(newPrefix.dropFirst(prefix.count))
            stable += MarkdownAnswerParser.parse(suffix + "XVIS_MARKDOWN_SENTINEL").dropLast()
            prefix = newPrefix
        }
        lastSource = source
        lastBlocks = stable + MarkdownAnswerParser.parse(String(source.dropFirst(prefix.count)))
        return lastBlocks
    }
    private static func boundary(_ source: String) -> String.Index {
        var lineStart = source.startIndex, cut = source.startIndex
        var fence: (Character, Int)?
        var blank = false
        while lineStart < source.endIndex {
            let newline = source[lineStart...].firstIndex(where: { $0 == "\n" || $0 == "\r\n" })
            let end = newline ?? source.endIndex
            let line = source[lineStart..<end].trimmingCharacters(in: .whitespacesAndNewlines)
            if fence == nil, blank, !line.isEmpty { cut = lineStart }
            if let open = fence {
                let count = line.prefix { $0 == open.0 }.count
                if count >= open.1 && line.dropFirst(count).trimmingCharacters(in: .whitespaces).isEmpty { fence = nil }
            } else if let first = line.first, first == "`" || first == "~" {
                let count = line.prefix { $0 == first }.count
                if count >= 3 && (first != "`" || !line.dropFirst(count).contains("`")) { fence = (first, count) }
            }
            blank = fence == nil && line.isEmpty
            guard let newline else { break }
            lineStart = source.index(after: newline)
        }
        return cut
    }
}

enum MarkdownTableAlignment: Equatable { case left, center, right }

enum MarkdownAnswerBlock: Equatable {
    case text(String)
    case heading(level: Int, text: String)
    case listItem(marker: String, text: String, indentation: Int)
    case quote(String)
    case divider
    case code(language: String?, text: String)
    case table(headers: [String], rows: [[String]], alignments: [MarkdownTableAlignment])
    case blank(Int)
}

enum MarkdownAnswerParser {
    static func parse(_ source: String) -> [MarkdownAnswerBlock] {
        let lines = source.components(separatedBy: "\n").map { $0.hasSuffix("\r") ? String($0.dropLast()) : $0 }
        var blocks: [MarkdownAnswerBlock] = []
        var paragraph: [String] = []
        var index = 0
        func flush() {
            if !paragraph.isEmpty { blocks.append(.text(paragraph.joined(separator: "\n"))); paragraph = [] }
        }
        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let fence = openingFence(trimmed) {
                flush()
                var end = index + 1
                while end < lines.count && !closingFence(lines[end], character: fence.character, count: fence.count) { end += 1 }
                guard end < lines.count else {
                    blocks.append(.text(lines[index...].joined(separator: "\n")))
                    break
                }
                let content = lines[(index + 1)..<end].joined(separator: "\n") + (end > index + 1 ? "\n" : "")
                blocks.append(.code(language: fence.language, text: content))
                index = end + 1
                continue
            }
            if index + 1 < lines.count, hasTablePipe(line), let aligns = tableAlignments(lines[index + 1]) {
                let header = tableCells(line)
                if header.count == aligns.count {
                    flush()
                    var rows: [[String]] = []
                    var end = index + 2
                    while end < lines.count && hasTablePipe(lines[end]) && !lines[end].trimmingCharacters(in: .whitespaces).isEmpty {
                        rows.append(tableCells(lines[end])); end += 1
                    }
                    // Retain extra cells instead of discarding data from an irregular row.
                    let count = max(header.count, rows.map(\.count).max() ?? 0)
                    let headers = header + Array(repeating: "", count: count - header.count)
                    let alignments = aligns + Array(repeating: .left, count: count - aligns.count)
                    blocks.append(.table(headers: headers, rows: rows.map { $0 + Array(repeating: "", count: count - $0.count) }, alignments: alignments))
                    index = end
                    continue
                }
            }
            if trimmed.isEmpty {
                flush()
                var count = 0
                while index < lines.count && lines[index].trimmingCharacters(in: .whitespaces).isEmpty { count += 1; index += 1 }
                // A final source newline is a terminator, rather than an extra empty paragraph.
                if index < lines.count { blocks.append(.blank(count)) }
                continue
            }
            if let heading = heading(trimmed) {
                flush(); blocks.append(.heading(level: heading.level, text: heading.text))
            } else if isDivider(trimmed) {
                flush(); blocks.append(.divider)
            } else if trimmed.hasPrefix(">") {
                flush()
                var quoted: [String] = []
                while index < lines.count {
                    let next = lines[index].trimmingCharacters(in: .whitespaces)
                    guard next.hasPrefix(">") else { break }
                    let content = next.dropFirst()
                    quoted.append(String(content.hasPrefix(" ") ? content.dropFirst() : content))
                    index += 1
                }
                blocks.append(.quote(quoted.joined(separator: "\n")))
                continue
            } else if let item = listItem(line) {
                flush(); blocks.append(.listItem(marker: item.marker, text: item.text, indentation: item.indentation))
            } else { paragraph.append(line) }
            index += 1
        }
        flush()
        return blocks
    }

    private static func openingFence(_ line: String) -> (character: Character, count: Int, language: String?)? {
        guard let first = line.first, first == "`" || first == "~" else { return nil }
        let count = line.prefix { $0 == first }.count
        guard count >= 3 else { return nil }
        let info = String(line.dropFirst(count)).trimmingCharacters(in: .whitespaces)
        guard first != "`" || !info.contains("`") else { return nil }
        return (first, count, info.isEmpty ? nil : info)
    }

    private static func closingFence(_ line: String, character: Character, count: Int) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        let prefix = trimmed.prefix { $0 == character }.count
        return prefix >= count && trimmed.dropFirst(prefix).trimmingCharacters(in: .whitespaces).isEmpty
    }

    private static func heading(_ line: String) -> (level: Int, text: String)? {
        let count = line.prefix { $0 == "#" }.count
        guard (1...6).contains(count), line.dropFirst(count).first == " " else { return nil }
        let text = line.dropFirst(count + 1).trimmingCharacters(in: .whitespaces)
        return (count, text)
    }

    private static func isDivider(_ line: String) -> Bool {
        let compact = line.filter { !$0.isWhitespace }
        guard compact.count >= 3, let first = compact.first, ["-", "*", "_"].contains(first) else { return false }
        return compact.allSatisfy { $0 == first }
    }

    private static func listItem(_ line: String) -> (marker: String, text: String, indentation: Int)? {
        let indentation = line.prefix { $0 == " " || $0 == "\t" }.reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
        let trimmed = line.drop { $0 == " " || $0 == "\t" }
        guard let first = trimmed.first else { return nil }
        if ["-", "+", "*"].contains(first), trimmed.dropFirst().first?.isWhitespace == true {
            return (String(first), trimmed.dropFirst().trimmingCharacters(in: .whitespaces), indentation)
        }
        let digits = trimmed.prefix { $0.isASCII && $0.isNumber }
        guard !digits.isEmpty, digits.count <= 9 else { return nil }
        let following = trimmed.dropFirst(digits.count)
        guard let suffix = following.first, suffix == "." || suffix == ")", following.dropFirst().first?.isWhitespace == true else { return nil }
        return (String(digits) + String(suffix), following.dropFirst().trimmingCharacters(in: .whitespaces), indentation)
    }

    private static func hasTablePipe(_ line: String) -> Bool { tableCells(line).count > 1 || line.trimmingCharacters(in: .whitespaces).hasPrefix("|") }

    private static func tableCells(_ line: String) -> [String] {
        let characters = Array(line.trimmingCharacters(in: .whitespaces))
        var cells = [""]
        var codeFence = 0
        var index = 0
        while index < characters.count {
            let character = characters[index]
            if character == "\\", index + 1 < characters.count, characters[index + 1] == "|" {
                cells[cells.count - 1].append("|"); index += 2; continue
            }
            if character == "`" {
                var end = index + 1
                while end < characters.count && characters[end] == "`" { end += 1 }
                let count = end - index
                if codeFence == 0 { codeFence = count } else if codeFence == count { codeFence = 0 }
                cells[cells.count - 1] += String(repeating: "`", count: count)
                index = end; continue
            }
            if character == "|" && codeFence == 0 { cells.append("") }
            else { cells[cells.count - 1].append(character) }
            index += 1
        }
        if characters.first == "|", cells.first == "" { cells.removeFirst() }
        if characters.last == "|", cells.last == "" { cells.removeLast() }
        return cells.map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func tableAlignments(_ line: String) -> [MarkdownTableAlignment]? {
        guard hasTablePipe(line) else { return nil }
        let cells = tableCells(line)
        guard !cells.isEmpty else { return nil }
        var alignments: [MarkdownTableAlignment] = []
        for cell in cells {
            let dashes = cell.trimmingCharacters(in: CharacterSet(charactersIn: ":"))
            guard dashes.count >= 3, dashes.allSatisfy({ $0 == "-" }) else { return nil }
            alignments.append(cell.hasPrefix(":") && cell.hasSuffix(":") ? .center : (cell.hasSuffix(":") ? .right : .left))
        }
        return alignments
    }
}

final class MarkdownAnswerCache {
    static let shared = MarkdownAnswerCache()
    private var storage: [String: [MarkdownAnswerBlock]] = [:]
    private var insertionOrder: [String] = []
    private let capacity: Int
    private(set) var hits = 0
    private var incremental: [String: MarkdownIncrementalCache] = [:]
    init(capacity: Int = 60) { self.capacity = max(1, capacity) }
    func blocks(for source: String, identity: String?) -> [MarkdownAnswerBlock] {
        guard let identity else { return blocks(for: source) }
        if incremental[identity] == nil {
            if incremental.count >= capacity, let key = incremental.keys.sorted().first { incremental.removeValue(forKey: key) }
            incremental[identity] = MarkdownIncrementalCache()
        }
        return incremental[identity]!.blocks(for: source)
    }
    func blocks(for source: String) -> [MarkdownAnswerBlock] {
        if let cached = storage[source] { hits += 1; return cached }
        let parsed = MarkdownAnswerParser.parse(source)
        storage[source] = parsed
        insertionOrder.append(source)
        if insertionOrder.count > capacity { storage.removeValue(forKey: insertionOrder.removeFirst()) }
        return parsed
    }
}

struct MarkdownAnswerView: View {
    let source: String
    var identity: String? = nil
    @ScaledMetric(relativeTo: .body) private var blankHeight = 10.0
    @ScaledMetric(relativeTo: .title2) private var firstHeadingSize = 26.0
    @ScaledMetric(relativeTo: .title3) private var secondHeadingSize = 22.0
    @ScaledMetric(relativeTo: .body) private var tableCellWidth = 150.0

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(MarkdownAnswerCache.shared.blocks(for: source, identity: identity).enumerated()), id: \.offset) { index, block in
                blockView(block)
                    .id((identity ?? "markdown") + "-answer-\(index)")
                    .background {
                        if let identity {
                            GeometryReader { geometry in
                                Color.clear.preference(key: ReadingBlockKey.self, value: [identity + "-answer-\(index)": geometry.frame(in: .named("xvis-conversation-scroll"))])
                            }
                        }
                    }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .foregroundStyle(XTheme.text)
        .tint(XTheme.accent)
    }

    @ViewBuilder private func blockView(_ block: MarkdownAnswerBlock) -> some View {
        switch block {
        case .text(let text):
            Text(inline(text)).font(.body).textSelection(.enabled).lineSpacing(5)
                .fixedSize(horizontal: false, vertical: true)
        case .heading(let level, let text):
            Text(inline(text)).font(headingFont(level))
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .padding(.top, level <= 2 ? 12 : 6).padding(.bottom, 4)
                .accessibilityAddTraits(.isHeader)
        case .listItem(let marker, let text, let indentation):
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Text(["-", "+", "*"].contains(marker) ? "•" : marker).font(.body).foregroundStyle(XTheme.accent).accessibilityHidden(true)
                Text(inline(text)).font(.body).textSelection(.enabled).lineSpacing(5)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, CGFloat(min(indentation, 24)) * 4)
        case .quote(let text):
            HStack(alignment: .top, spacing: 12) {
                RoundedRectangle(cornerRadius: 2).fill(XTheme.accent).frame(width: 2)
                Text(inline(text)).font(.body).foregroundStyle(XTheme.secondary).textSelection(.enabled).lineSpacing(5)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .fixedSize(horizontal: false, vertical: true).padding(.vertical, 6)
        case .divider:
            Rectangle().fill(XTheme.border).frame(height: 1).padding(.vertical, 10).accessibilityHidden(true)
        case .blank(let count):
            Color.clear.frame(height: blankHeight * Double(min(count, 3))).accessibilityHidden(true)
        case .code(let language, let text):
            MarkdownCodeBlock(language: language, text: text).padding(.vertical, 6)
        case .table(let headers, let rows, let alignments):
            VStack(alignment: .leading, spacing: 6) {
                Label("表格可横向滑动", systemImage: "arrow.left.and.right")
                    .font(.caption).foregroundStyle(XTheme.secondary)
                ScrollView(.horizontal) {
                    Grid(horizontalSpacing: 0, verticalSpacing: 0) {
                        tableRow(headers, alignments: alignments, header: true)
                        ForEach(Array(rows.enumerated()), id: \.offset) { _, cells in tableRow(cells, alignments: alignments, header: false) }
                    }
                }
                .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
                .background(XTheme.surface)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay { RoundedRectangle(cornerRadius: 12).strokeBorder(XTheme.border, lineWidth: 0.75) }
            }
            .padding(.vertical, 6)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        if level == 1 { return .system(size: firstHeadingSize, weight: .semibold) }
        if level == 2 { return .system(size: secondHeadingSize, weight: .semibold) }
        return .headline
    }

    private func tableRow(_ cells: [String], alignments: [MarkdownTableAlignment], header: Bool) -> some View {
        GridRow {
            ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
                Text(inline(cell)).font(header ? .body.weight(.semibold) : .body).textSelection(.enabled)
                    .multilineTextAlignment(textAlignment(alignments[index]))
                    .frame(width: tableCellWidth, alignment: alignment(alignments[index]))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(11)
                    .background(header ? XTheme.soft : XTheme.surface)
                    .overlay { Rectangle().stroke(XTheme.border, lineWidth: 0.5) }
            }
        }
    }

    private func inline(_ source: String) -> AttributedString {
        // Raw HTML remains readable text. Inline parsing never loads remote images.
        guard source.range(of: "<[A-Za-z!/][^>]*>", options: .regularExpression) == nil else { return AttributedString(source) }
        var result = (try? AttributedString(markdown: source, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(source)
        for run in result.runs {
            if let url = run.link, !["https", "http"].contains(url.scheme?.lowercased() ?? "") { result[run.range].link = nil }
            if run.inlinePresentationIntent?.contains(.code) == true { result[run.range].font = .system(.body, design: .monospaced) }
        }
        return result
    }

    private func alignment(_ value: MarkdownTableAlignment) -> Alignment {
        switch value { case .left: return .leading; case .center: return .center; case .right: return .trailing }
    }
    private func textAlignment(_ value: MarkdownTableAlignment) -> TextAlignment {
        switch value { case .left: return .leading; case .center: return .center; case .right: return .trailing }
    }
}

private struct MarkdownCodeBlock: View {
    let language: String?
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text(language ?? "代码").font(.caption.weight(.medium)).foregroundStyle(XTheme.secondary)
                Spacer(minLength: 0)
                ClipboardButton(text: text, label: "复制", accessibility: "复制代码")
            }
            .padding(.horizontal, 12)
            Rectangle().fill(XTheme.border).frame(height: 0.75).accessibilityHidden(true)
            ScrollView(.horizontal) {
                Text(text).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                    .foregroundStyle(XTheme.text)
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(14)
            }
            .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
        }
        .background(XTheme.surface)
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(XTheme.border, lineWidth: 0.75) }
    }
}
