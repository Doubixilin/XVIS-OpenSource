import SwiftUI
import VisionKit
import AVFoundation

struct PairingLink {
    let requestID: String
    let secret: String
    init(_ text: String, expectedServer: URL) throws {
        guard let parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme == "xvis-pair", parts.host == "device", parts.path.isEmpty,
              parts.user == nil, parts.password == nil, parts.fragment == nil, parts.port == nil,
              let items = parts.queryItems, items.count == 3, Set(items.map(\.name)) == Set(["server", "id", "secret"]),
              let server = items.first(where: { $0.name == "server" })?.value,
              try APIClient.validatedURL(server) == expectedServer,
              let id = items.first(where: { $0.name == "id" })?.value, XNotificationID.matches(id, prefix: "p"),
              let secret = items.first(where: { $0.name == "secret" })?.value,
              secret.range(of: "^[A-Za-z0-9_-]{32,128}$", options: .regularExpression) != nil else {
            throw AppFailure.network("配对链接无效或属于其他中枢，请核对电脑上的二维码。")
        }
        self.requestID = id; self.secret = secret
    }
}
struct PairingPreview: Decodable {
    var request_id: String
    var name: String
    var fingerprint: String
    var expires_at: Double
    var node_id: String?
    var status: String
    var isValid: Bool {
        XNotificationID.matches(request_id, prefix: "p") && !name.isEmpty && name.count <= 80
        && fingerprint.range(of: "^[0-9a-f]{12}$", options: .regularExpression) != nil
        && expires_at.isFinite && ["pending", "approved"].contains(status)
        && (node_id == nil || XNotificationID.matches(node_id ?? "", prefix: "n"))
    }
}
struct PairedNode: Decodable, Identifiable {
    var node_id: String
    var name: String
    var created_at: String
    var revoked_at: String?
    var id: String { node_id }
}
struct PairedNodes: Decodable { var nodes: [PairedNode] }
struct PairingRevoked: Decodable { var node_id: String; var revoked: Bool }

struct DevicePairingView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var preview: PairingPreview?
    @State private var nodes: [PairedNode] = []
    @State private var note: String?
    @State private var busy = false
    @State private var scanning = false
    @State private var revoking: PairedNode?
    var body: some View {
        NavigationStack {
            Form {
                Section("接入新设备") {
                    Text("在新设备运行配对命令，扫描其二维码或粘贴配对链接。请核对设备名和指纹后连接。")
                        .font(.footnote).foregroundStyle(XTheme.secondary)
                    Button { openScanner() } label: { Label("扫描设备二维码", systemImage: "qrcode.viewfinder") }
                    TextField("粘贴配对链接", text: $text, axis: .vertical)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .lineLimit(2...4).accessibilityIdentifier("device-pairing-link")
                    PasteButton(payloadType: String.self) { if let value = $0.first { text = value } }
                    Button(busy ? "正在核对…" : "核对设备") {
                        busy = true; note = nil; preview = nil
                        Task {
                            defer { busy = false }
                            do { preview = try await store.inspectPairing(text) }
                            catch { note = error.localizedDescription }
                        }
                    }.disabled(busy || text.isEmpty)
                }.listRowBackground(XTheme.surface)
                if let preview {
                    Section("确认授权") {
                        LabeledContent("设备", value: preview.name)
                        LabeledContent("核对指纹", value: preview.fingerprint).monospaced()
                        Text("有效至 \(Date(timeIntervalSince1970: preview.expires_at).formatted(date: .omitted, time: .shortened))")
                            .font(.footnote).foregroundStyle(XTheme.secondary)
                        Text("此设备获得独立连接凭证，只执行你明确交给它的任务；随后可以单独撤销。")
                            .font(.footnote)
                        Button("连接此设备") {
                            busy = true; note = nil
                            Task {
                                defer { busy = false }
                                do {
                                    _ = try await store.approvePairing(text, expected: preview)
                                    self.preview = nil; text = ""; note = "授权已完成，等待新设备连接。"
                                    nodes = try await store.pairedNodes()
                                } catch { note = error.localizedDescription }
                            }
                        }.disabled(busy || preview.expires_at <= Date().timeIntervalSince1970)
                    }.listRowBackground(XTheme.surface)
                }
                if let note { Section { Text(note).font(.footnote).foregroundStyle(XTheme.secondary) }.listRowBackground(XTheme.surface) }
                Section("已授予的设备") {
                    if nodes.isEmpty { Text("还没有通过扫码授予的设备。原有配置连接继续保留。").font(.footnote).foregroundStyle(XTheme.secondary) }
                    ForEach(nodes) { node in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(node.name).font(.headline)
                            Text(node.revoked_at == nil ? "独立授权" : "已撤销").font(.caption).foregroundStyle(XTheme.secondary)
                            if node.revoked_at == nil { Button("撤销授权", role: .destructive) { revoking = node }.disabled(busy) }
                        }.padding(.vertical, 4)
                    }
                }.listRowBackground(XTheme.surface)
            }
            .scrollContentBackground(.hidden).background(XTheme.background).tint(XTheme.accent)
            .navigationTitle("设备连接").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .task { do { nodes = try await store.pairedNodes() } catch { note = error.localizedDescription } }
            .onChange(of: text) { _, _ in preview = nil }
            .sheet(isPresented: $scanning) {
                NavigationStack {
                    PairingScanner(found: { text = $0; scanning = false }, failed: {
                        scanning = false; note = "扫码暂不可用，请重试或粘贴配对链接。"
                    })
                        .navigationTitle("扫描设备二维码").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { scanning = false } } }
                }
            }
            .confirmationDialog("撤销此设备的连接授权？", isPresented: Binding(get: { revoking != nil }, set: { if !$0 { revoking = nil } }), titleVisibility: .visible) {
                if let node = revoking {
                    Button("撤销 \(node.name)", role: .destructive) {
                        busy = true
                        Task {
                            defer { busy = false; revoking = nil }
                            do { try await store.revokePairing(node.id); nodes = try await store.pairedNodes() }
                            catch { note = error.localizedDescription }
                        }
                    }
                }
            } message: { Text("设备将无法再连接中枢。已开始的本机进程可能继续；需要停止时请先取消任务。") }
        }
    }
    private func openScanner() {
        guard DataScannerViewController.isSupported else { note = "此设备暂不支持相机扫码，可以粘贴同一配对链接。"; return }
        Task {
            let allowed = await AVCaptureDevice.requestAccess(for: .video)
            if allowed && DataScannerViewController.isAvailable { scanning = true }
            else { note = "相机暂不可用，请在系统设置允许相机访问，或粘贴配对链接。" }
        }
    }
}

private struct PairingScanner: UIViewControllerRepresentable {
    let found: (String) -> Void
    let failed: () -> Void
    func makeCoordinator() -> Coordinator { Coordinator(found: found, failed: failed) }
    func makeUIViewController(context: Context) -> DataScannerViewController {
        let controller = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced, recognizesMultipleItems: false, isGuidanceEnabled: true, isHighlightingEnabled: true)
        controller.delegate = context.coordinator
        do { try controller.startScanning() }
        catch { DispatchQueue.main.async { failed() } }
        return controller
    }
    func updateUIViewController(_ controller: DataScannerViewController, context: Context) { }
    static func dismantleUIViewController(_ controller: DataScannerViewController, coordinator: Coordinator) { controller.stopScanning() }
    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let found: (String) -> Void
        let failed: () -> Void
        private var delivered = false
        init(found: @escaping (String) -> Void, failed: @escaping () -> Void) { self.found = found; self.failed = failed }
        func dataScanner(_ dataScanner: DataScannerViewController, becameUnavailableWithError error: DataScannerViewController.ScanningUnavailable) {
            guard !delivered else { return }; delivered = true
            dataScanner.stopScanning(); failed()
        }
        func dataScanner(_ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            for case .barcode(let item) in addedItems {
                if let text = item.payloadStringValue, !delivered {
                    delivered = true; dataScanner.stopScanning(); found(text); return
                }
            }
        }
    }
}
