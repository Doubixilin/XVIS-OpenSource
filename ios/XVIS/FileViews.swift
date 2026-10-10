import SwiftUI
import QuickLook
import UIKit

struct TaskFilesCard: View {
    let task: TaskView
    @EnvironmentObject private var store: AppStore
    @State private var refreshing = false
    @State private var retrying = false
    @State private var message: String?
    private var files: [OutputFile] { Array((task.output_files ?? []).prefix(4)) }
    private var transfer: FileTransfer { task.file_transfer ?? FileTransfer(state: "unknown", note: nil) }
    private var visible: Bool {
        guard task.output_files != nil || task.file_transfer != nil else { return false }
        return !files.isEmpty || !(transfer.note ?? "").isEmpty || !["none", "unknown"].contains(transfer.state)
    }

    var body: some View {
        if visible {
            VStack(alignment: .leading, spacing: 12) {
                Label("文件成果", systemImage: "doc.text")
                    .font(.subheadline.weight(.semibold))
                Text(transfer.label).font(.footnote).foregroundStyle(transfer.state == "ready" ? XTheme.secondary : XTheme.warning)
                if let note = transfer.note, !note.isEmpty {
                    Text(note).font(.footnote).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(files) { file in OutputFileRow(file: file, taskID: task.id) }
                if let message {
                    Text(message).font(.footnote).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if ["pending", "partial", "failed", "unknown"].contains(transfer.state) {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 12) { actions }
                        VStack(alignment: .leading, spacing: 2) { actions }
                    }
                    .buttonStyle(XPressButtonStyle()).font(.footnote.weight(.medium)).foregroundStyle(XTheme.accent)
                }
                if !files.isEmpty {
                    Text("领取时核对文件长度与摘要；这表示传输一致，内容仍需检查。")
                        .font(.caption).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .background(XTheme.surface, in: RoundedRectangle(cornerRadius: 16))
            .overlay { RoundedRectangle(cornerRadius: 16).strokeBorder(XTheme.border, lineWidth: 0.75) }
        }
    }
    @ViewBuilder private var actions: some View {
        Button(refreshing ? "正在刷新…" : "刷新文件状态") {
            refreshing = true; message = nil
            Task {
                defer { refreshing = false }
                do { _ = try await store.refreshTaskFiles(task.id) }
                catch { message = error.localizedDescription }
            }
        }.disabled(refreshing || retrying)
        .accessibilityLabel("刷新本轮文件状态")
        if ["pending", "partial", "failed"].contains(transfer.state) {
            Button(retrying ? "正在请求…" : "请求补传") {
                retrying = true; message = nil
                Task {
                    defer { retrying = false }
                    do {
                        let reply = try await store.retryFileDelivery(taskID: task.id)
                        message = reply.recorded ? (reply.node_online ? "已请求补传，稍后刷新查看。" : "已保存补传请求，等待执行设备上线。") : "当前没有需要补传的文件，请刷新状态。"
                    } catch { message = error.localizedDescription }
                }
            }.disabled(refreshing || retrying)
            .accessibilityHint("只补传固定的文件副本，不会重新执行任务")
        }
    }
}

private struct OutputFileRow: View {
    let file: OutputFile
    let taskID: String
    @EnvironmentObject private var store: AppStore
    @State private var loading = false
    @State private var message: String?
    @State private var preview: FilePreviewContent?
    @State private var share: FilePreviewContent?
    @State private var canRequestRepair = false
    @State private var repairing = false
    @State private var repairNote: String?
    private var size: String { ByteCountFormatter.string(fromByteCount: Int64(file.size_bytes), countStyle: .file) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: file.isText ? "doc.plaintext" : "doc.richtext")
                    .font(.title3).foregroundStyle(XTheme.accent).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(file.filename).font(.subheadline.weight(.medium))
                        .fixedSize(horizontal: false, vertical: true)
                    Text("\((file.filename as NSString).pathExtension.uppercased()) · \(size) · \(file.statusLabel)")
                        .font(.caption).foregroundStyle(XTheme.secondary)
                }
                Spacer(minLength: 0)
                if loading { ProgressView().tint(XTheme.accent).accessibilityLabel("正在下载文件") }
            }
            if let reason = file.error, !reason.isEmpty {
                Text(reason).font(.caption).foregroundStyle(XTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !file.isValid {
                Text("文件信息不完整，暂时无法领取。请刷新本轮记录。")
                    .font(.caption).foregroundStyle(XTheme.warning)
            } else if file.status == "ready" {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { actions }
                    VStack(alignment: .leading, spacing: 2) { actions }
                }
                .disabled(loading).buttonStyle(XPressButtonStyle()).font(.footnote.weight(.medium)).foregroundStyle(XTheme.accent)
            }
            if let message {
                Text(message).font(.caption).foregroundStyle(XTheme.error)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let repairNote {
                Text(repairNote).font(.caption).foregroundStyle(XTheme.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if canRequestRepair {
                Button(repairing ? "正在请求补传…" : "请求补传此轮文件") { Task { await requestRepair() } }
                    .font(.footnote.weight(.medium)).foregroundStyle(XTheme.accent)
                    .frame(minHeight: 44).buttonStyle(XPressButtonStyle()).disabled(loading || repairing)
                    .accessibilityLabel("请求补传文件 \(file.filename)")
                    .accessibilityHint("检查并补传本轮固定文件副本，不会重新执行模型任务")
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(XTheme.soft, in: RoundedRectangle(cornerRadius: 12))
        .sheet(item: $preview) { XFilePreview(content: $0) }
        .sheet(item: $share) { XFileActivity(url: $0.url) }
    }
    @ViewBuilder private var actions: some View {
        Button("预览文件") { Task { await open(sharing: false, force: false) } }
            .frame(minHeight: 44)
            .accessibilityLabel("预览文件 \(file.filename)")
        Button("分享") { Task { await open(sharing: true, force: false) } }
            .frame(minHeight: 44)
            .accessibilityLabel("分享文件 \(file.filename)")
        Button("重新下载") { Task { await open(sharing: false, force: true) } }
            .frame(minHeight: 44)
            .accessibilityLabel("重新下载文件 \(file.filename)")
    }
    private func open(sharing: Bool, force: Bool) async {
        guard !loading, !repairing else { return }
        loading = true; message = nil; repairNote = nil
        defer { loading = false }
        do {
            let url = try await store.loadFile(file, taskID: taskID, forceDownload: force)
            guard !Task.isCancelled else { return }
            let content = FilePreviewContent(file: file, url: url)
            canRequestRepair = false
            if sharing { share = content } else { preview = content }
        } catch {
            if !(error is CancellationError) {
                message = "\(error.localizedDescription) 可重新下载或请求补传，任务不会重跑。"
                canRequestRepair = true
            }
        }
    }
    private func requestRepair() async {
        guard !loading, !repairing else { return }
        repairing = true; repairNote = nil
        defer { repairing = false }
        do {
            let reply = try await store.retryFileDelivery(taskID: taskID)
            guard !Task.isCancelled else { return }
            message = nil; canRequestRepair = false
            repairNote = reply.recorded ? (reply.node_online ? "已请求补传。刷新文件状态后可重新下载，任务不会重跑。" : "已保存补传请求，等待执行设备上线；任务不会重跑。") : "文件检查已完成。可重新下载，或刷新本轮文件状态。"
        } catch { if !(error is CancellationError) { message = error.localizedDescription } }
    }
}

struct FilePreviewContent: Identifiable {
    let file: OutputFile
    let url: URL
    var id: String { file.id }
}

private struct XFilePreview: View {
    let content: FilePreviewContent
    @Environment(\.dismiss) private var dismiss
    @State private var text: String?
    @State private var truncated = false
    @State private var share = false
    @State private var previewable = true
    var body: some View {
        NavigationStack {
            Group {
                if content.file.isText {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 12) {
                            if truncated {
                                Label("文件较长，此处预览前 256 KB。分享可取得完整文件。", systemImage: "info.circle")
                                    .font(.footnote).foregroundStyle(XTheme.warning)
                            }
                            Text(text ?? "正在打开文件…").font(.body.monospaced()).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }.padding(20)
                    }.background(XTheme.background).foregroundStyle(XTheme.text)
                } else if previewable {
                    XQuickLook(url: content.url).background(XTheme.background)
                } else {
                    ContentUnavailableView("系统暂时无法预览此文件", systemImage: "doc",
                                           description: Text("文件已核对传输完整性，可以分享后在其他应用打开。"))
                }
            }
            .navigationTitle(content.file.filename).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { share = true } label: { Image(systemName: "square.and.arrow.up") }
                        .accessibilityLabel("分享文件 \(content.file.filename)")
                }
            }
            .task {
                if content.file.isText {
                    do {
                        let handle = try FileHandle(forReadingFrom: content.url)
                        defer { try? handle.close() }
                        let bytes = try handle.read(upToCount: 256 * 1024) ?? Data()
                        truncated = content.file.size_bytes > bytes.count
                        text = String(decoding: bytes, as: UTF8.self)
                    } catch { text = "暂时无法读取预览，请关闭后重新下载。" }
                } else { previewable = QLPreviewController.canPreview(content.url as NSURL) }
            }
            .sheet(isPresented: $share) { XFileActivity(url: content.url) }
        }.tint(XTheme.accent)
    }
}

private struct XQuickLook: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController(); controller.dataSource = context.coordinator
        return controller
    }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) { }
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}

private struct XFileActivity: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) { }
}
