import SwiftUI
import PhotosUI
import UIKit
import ImageIO

struct ImageComposer: View {
    let taskID: String?
    var onProcessingChanged: (Bool) -> Void = { _ in }
    @EnvironmentObject private var store: AppStore
    @State private var selectedItems: [PhotosPickerItem] = []
    @State private var importTask: Task<Void, Never>?
    @State private var importToken: UUID?
    @State private var processingLabel: String?
    @State private var importMessage: String?
    @State private var viewer: XImageViewerContent?

    private var drafts: [PhotoDraft] { store.imageDrafts(for: taskID) }
    private var processing: Bool { processingLabel != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                PhotosPicker(selection: $selectedItems,
                             maxSelectionCount: max(1, 4 - drafts.count),
                             selectionBehavior: .ordered, matching: .images,
                             preferredItemEncoding: .compatible) {
                    Label("添加图片", systemImage: "photo.badge.plus")
                        .font(.subheadline)
                        .foregroundStyle(XTheme.accent)
                        .frame(minHeight: 44)
                }
                .buttonStyle(XPressButtonStyle())
                .disabled(processing || drafts.count >= 4)
                .accessibilityLabel(drafts.count >= 4 ? "已添加四张图片" : "从相册添加图片")
                .accessibilityHint("最多四张，添加后可预览和删除")
                Spacer(minLength: 0)
                if !drafts.isEmpty {
                    Text("\(drafts.count)/4")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(XTheme.secondary)
                        .accessibilityLabel("已添加 \(drafts.count) 张，最多四张")
                }
            }
            if let processingLabel {
                HStack(alignment: .top, spacing: 8) {
                    ProgressView().tint(XTheme.accent).accessibilityHidden(true)
                    Text(processingLabel).font(.caption).foregroundStyle(XTheme.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
            if let importMessage {
                Text(importMessage).font(.caption).foregroundStyle(XTheme.error)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !drafts.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 10) {
                        ForEach(Array(drafts.enumerated()), id: \.element.id) { index, draft in
                            XDraftImageTile(draft: draft, taskID: taskID, index: index) { viewer = $0 }
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .onChange(of: selectedItems) { _, items in
            if !items.isEmpty { importImages(items) }
        }
        .onChange(of: taskID) { _, _ in cancelImport() }
        .onDisappear { cancelImport() }
        .fullScreenCover(item: $viewer) { XImageViewer(content: $0) }
    }

    private func importImages(_ items: [PhotosPickerItem]) {
        guard !processing else { return }
        let destination = taskID
        let token = UUID()
        let chosen = Array(items.prefix(max(0, 4 - drafts.count)))
        importToken = token
        importMessage = nil
        processingLabel = "正在读取相册图片…"
        onProcessingChanged(true)
        importTask = Task { @MainActor in
            var failures = 0
            for (index, item) in chosen.enumerated() {
                guard !Task.isCancelled, importToken == token else { break }
                processingLabel = "正在处理第 \(index + 1)/\(chosen.count) 张图片…"
                do {
                    guard let data = try await item.loadTransferable(type: Data.self) else {
                        throw XImageError.unreadable
                    }
                    let prepared = try await Task.detached(priority: .userInitiated) {
                        try XImageDecoding.normalizedJPEG(data)
                    }.value
                    guard !Task.isCancelled, importToken == token else { break }
                    guard store.imageDrafts(for: destination).count < 4 else { break }
                    try store.addImage(data: prepared, for: destination)
                } catch {
                    guard !Task.isCancelled, importToken == token else { break }
                    failures += 1
                }
            }
            guard importToken == token else { return }
            if failures > 0 {
                importMessage = "\(failures) 张图片未能读取或处理，请重新选择。已添加的图片会保留。"
            }
            importToken = nil
            processingLabel = nil
            selectedItems = []
            importTask = nil
            onProcessingChanged(false)
        }
    }

    private func cancelImport() {
        importTask?.cancel()
        importTask = nil
        importToken = nil
        processingLabel = nil
        selectedItems = []
        importMessage = nil
        onProcessingChanged(false)
    }
}

private struct XDraftImageTile: View {
    let draft: PhotoDraft
    let taskID: String?
    let index: Int
    var open: (XImageViewerContent) -> Void
    @EnvironmentObject private var store: AppStore
    @State private var thumbnail: UIImage?
    @State private var unreadable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ZStack(alignment: .topTrailing) {
                Button(action: openImage) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 14).fill(XTheme.soft)
                        if let thumbnail {
                            Image(uiImage: thumbnail).resizable().scaledToFill()
                        } else {
                            Image(systemName: unreadable ? "exclamationmark.triangle" : "photo")
                                .foregroundStyle(XTheme.secondary)
                        }
                    }
                    .frame(width: 104, height: 84)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(XTheme.border, lineWidth: 0.75) }
                }
                .buttonStyle(XPressButtonStyle())
                .accessibilityLabel(unreadable ? "第 \(index + 1) 张草稿图片无法读取，轻点重试" : "查看第 \(index + 1) 张草稿图片")
                Button { store.removeImage(id: draft.id, for: taskID) } label: {
                    Image(systemName: "xmark").font(.caption.weight(.semibold))
                        .foregroundStyle(XTheme.text)
                        .frame(width: 44, height: 44)
                        .background(XTheme.surface, in: Circle())
                        .overlay { Circle().strokeBorder(XTheme.border, lineWidth: 0.75) }
                }
                .buttonStyle(XPressButtonStyle())
                .accessibilityLabel("删除第 \(index + 1) 张草稿图片")
            }
            Text(unreadable ? "无法读取" : "图片 \(index + 1)")
                .font(.caption).foregroundStyle(unreadable ? XTheme.error : XTheme.secondary)
        }
        .task(id: draft.id) { loadThumbnail() }
    }

    private func loadThumbnail() {
        do {
            let data = try store.imageData(for: draft)
            guard let image = XImageDecoding.thumbnail(data, maxPixelSize: 320) else { throw XImageError.unreadable }
            thumbnail = image
            unreadable = false
        } catch {
            thumbnail = nil
            unreadable = true
        }
    }

    private func openImage() {
        do {
            let data = try store.imageData(for: draft)
            guard XImageDecoding.thumbnail(data, maxPixelSize: 64) != nil else { throw XImageError.unreadable }
            unreadable = false
            open(XImageViewerContent(id: draft.id, data: data, mimeType: try ImageDisk.contentType(data), title: "图片 \(index + 1)"))
        } catch {
            unreadable = true
            loadThumbnail()
        }
    }
}

struct TaskImages: View {
    let images: [ImageView]
    let taskID: String
    let openImage: (XImageViewerContent) -> Void

    var body: some View {
        if !images.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(Array(images.enumerated()), id: \.element.id) { index, image in
                        XTaskImageTile(image: image, taskID: taskID, index: index, open: openImage)
                    }
                }
                .padding(.vertical, 4)
            }
        }
    }
}

private struct XTaskImageTile: View {
    let image: ImageView
    let taskID: String
    let index: Int
    var open: (XImageViewerContent) -> Void
    @EnvironmentObject private var store: AppStore
    @State private var thumbnail: UIImage?
    @State private var loading = true
    @State private var failed = false
    @State private var opening = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                Task { await openImage() }
            } label: {
                ZStack {
                    RoundedRectangle(cornerRadius: 16).fill(XTheme.soft)
                    if let thumbnail {
                        Image(uiImage: thumbnail).resizable().scaledToFill()
                    } else if failed {
                        Label("图片未加载", systemImage: "exclamationmark.triangle")
                            .font(.caption).foregroundStyle(XTheme.secondary)
                            .padding(10)
                    }
                    if loading || opening {
                        ProgressView().tint(XTheme.accent)
                            .padding(12).background(XTheme.surface, in: Circle())
                    }
                }
                .frame(width: 148, height: 112)
                .clipShape(RoundedRectangle(cornerRadius: 16))
                .overlay { RoundedRectangle(cornerRadius: 16).strokeBorder(XTheme.border, lineWidth: 0.75) }
            }
            .buttonStyle(XPressButtonStyle())
            .disabled(thumbnail == nil || opening)
            .accessibilityLabel(loading ? "正在加载第 \(index + 1) 张图片" : "查看第 \(index + 1) 张图片")
            .accessibilityHint("全屏查看或分享图片")
            if failed {
                Button("重试加载") { Task { await loadThumbnail() } }
                    .font(.caption).foregroundStyle(XTheme.accent)
                    .frame(minWidth: 44, minHeight: 44)
                    .buttonStyle(XPressButtonStyle())
                    .accessibilityLabel("重试加载第 \(index + 1) 张图片")
                    .disabled(loading)
            } else {
                Text("图片 \(index + 1)").font(.caption).foregroundStyle(XTheme.secondary)
            }
        }
        .frame(width: 148, alignment: .leading)
        .task(id: image.id) { await loadThumbnail() }
    }

    private func loadThumbnail() async {
        loading = true
        failed = false
        do {
            let data = try await store.loadImage(image, taskID: taskID)
            guard !Task.isCancelled else { return }
            guard let value = XImageDecoding.thumbnail(data, maxPixelSize: 480) else { throw XImageError.unreadable }
            thumbnail = value
        } catch {
            guard !Task.isCancelled else { return }
            thumbnail = nil
            failed = true
        }
        loading = false
    }

    private func openImage() async {
        guard !opening else { return }
        opening = true
        defer { opening = false }
        do {
            let data = try await store.loadImage(image, taskID: taskID)
            guard !Task.isCancelled else { return }
            guard XImageDecoding.thumbnail(data, maxPixelSize: 64) != nil else { throw XImageError.unreadable }
            failed = false
            open(XImageViewerContent(id: image.id, data: data, mimeType: image.mime_type, title: "图片 \(index + 1)"))
        } catch {
            guard !Task.isCancelled else { return }
            failed = true
        }
    }
}

struct XImageViewerContent: Identifiable {
    let id: String
    let data: Data
    let mimeType: String
    let title: String
}

struct XImageViewer: View {
    let content: XImageViewerContent
    @Environment(\.dismiss) private var dismiss
    @State private var displayImage: UIImage?
    @State private var decoding = true
    @State private var share: XImageShareFile?
    @State private var temporaryShareURL: URL?
    @State private var shareFailed = false

    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                ZStack {
                    XTheme.background
                    if let displayImage {
                        Image(uiImage: displayImage).resizable().scaledToFit()
                            .frame(width: geometry.size.width, height: geometry.size.height)
                            .accessibilityLabel(content.title)
                    } else if decoding {
                        ProgressView("正在打开图片…").tint(XTheme.accent).foregroundStyle(XTheme.secondary)
                    } else {
                        ContentUnavailableView("无法查看图片", systemImage: "exclamationmark.triangle",
                                               description: Text("请关闭后重新加载。"))
                    }
                }
            }
            .background(XTheme.background)
            .navigationTitle(content.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(XTheme.background, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                        .buttonStyle(XIconButtonStyle()).accessibilityLabel("关闭图片")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(action: prepareShare) { Image(systemName: "square.and.arrow.up") }
                        .buttonStyle(XIconButtonStyle()).accessibilityLabel("分享图片")
                        .disabled(displayImage == nil)
                }
            }
            .task(id: content.id) {
                displayImage = XImageDecoding.thumbnail(content.data, maxPixelSize: 4096)
                decoding = false
            }
            .sheet(item: $share, onDismiss: removeShareFile) { XImageActivity(fileURL: $0.url) }
            .alert("无法分享图片", isPresented: $shareFailed) {
                Button("知道了", role: .cancel) { }
            } message: {
                Text("暂时无法准备图片，请重试。")
            }
            .onDisappear { if share == nil { removeShareFile() } }
        }
        .tint(XTheme.accent)
    }

    private func prepareShare() {
        removeShareFile()
        let suffix: String
        switch content.mimeType {
        case "image/png": suffix = "png"
        case "image/webp": suffix = "webp"
        case "image/heic", "image/heif": suffix = "heic"
        default: suffix = "jpg"
        }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("XVIS-\(UUID().uuidString).\(suffix)")
        do {
            try content.data.write(to: url, options: [.atomic, .completeFileProtection])
            temporaryShareURL = url
            share = XImageShareFile(url: url)
        } catch {
            try? FileManager.default.removeItem(at: url)
            shareFailed = true
        }
    }

    private func removeShareFile() {
        if let url = temporaryShareURL { try? FileManager.default.removeItem(at: url) }
        temporaryShareURL = nil
    }
}

private struct XImageShareFile: Identifiable {
    let url: URL
    var id: String { url.lastPathComponent }
}

private struct XImageActivity: UIViewControllerRepresentable {
    let fileURL: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [XImageShareSource(fileURL)], applicationActivities: nil)
    }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) { }
}

private final class XImageShareSource: NSObject, UIActivityItemSource {
    private let url: URL
    private let image: UIImage?
    init(_ url: URL) { self.url = url; self.image = UIImage(contentsOfFile: url.path) }
    func activityViewControllerPlaceholderItem(_ activityViewController: UIActivityViewController) -> Any {
        image.map { $0 as Any } ?? url
    }
    func activityViewController(_ activityViewController: UIActivityViewController,
                                itemForActivityType activityType: UIActivity.ActivityType?) -> Any? {
        // Photos receives an image; file and messaging activities retain the original bytes.
        if activityType == .saveToCameraRoll, let image { return image }
        return url
    }
    func activityViewController(_ activityViewController: UIActivityViewController,
                                dataTypeIdentifierForActivityType activityType: UIActivity.ActivityType?) -> String {
        url.pathExtension == "png" ? "public.png" : "public.jpeg"
    }
}

private enum XImageError: Error { case unreadable, tooLarge }

private enum XImageDecoding {
    static func thumbnail(_ data: Data, maxPixelSize: Int) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
                kCGImageSourceShouldCacheImmediately: true
              ] as CFDictionary) else { return nil }
        return UIImage(cgImage: image)
    }

    // Decode at the target size, then redraw into a fresh JPEG to remove source metadata.
    static func normalizedJPEG(_ data: Data) throws -> Data {
        guard let image = thumbnail(data, maxPixelSize: 2048),
              image.size.width > 0, image.size.height > 0 else { throw XImageError.unreadable }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let redrawn = UIGraphicsImageRenderer(size: image.size, format: format).image { context in
            UIColor.white.setFill()
            context.fill(CGRect(origin: .zero, size: image.size))
            image.draw(in: CGRect(origin: .zero, size: image.size))
        }
        let qualities: [CGFloat] = [0.88, 0.72, 0.55, 0.40]
        for quality in qualities {
            if let result = redrawn.jpegData(compressionQuality: quality), result.count <= 6 * 1024 * 1024 {
                return result
            }
        }
        throw XImageError.tooLarge
    }
}
