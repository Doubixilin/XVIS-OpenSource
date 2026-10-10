import Foundation
import CryptoKit

struct OutputFile: Codable, Identifiable, Equatable {
    var file_id: String
    var filename: String
    var mime_type: String
    var size_bytes: Int
    var sha256: String
    var status: String
    var error: String?
    var id: String { file_id }
    var isText: Bool { mime_type.hasPrefix("text/") }
    var statusLabel: String {
        switch status { case "ready": return "可领取"; case "pending": return "正在交付"; default: return "未交付" }
    }
    var isValid: Bool {
        guard XNotificationID.matches(file_id, prefix: "f"), !filename.isEmpty,
              filename.utf8.count <= 180, filename != ".", filename != "..",
              !filename.contains("/"), !filename.contains("\\"),
              !filename.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              size_bytes > 0, size_bytes <= FileDisk.byteLimit,
              sha256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              ["pending", "ready", "failed"].contains(status) else { return false }
        let types = ["txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "pdf": "application/pdf",
                     "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
        return types[(filename as NSString).pathExtension.lowercased()] == mime_type
    }
}

struct FileTransfer: Codable, Equatable {
    var state: String
    var note: String?
    var label: String {
        switch state {
        case "unknown": return "尚未收到文件交付信息"
        case "none": return "本轮没有文件成果"
        case "pending": return "文件正在交付"
        case "ready": return "文件已交付"
        case "partial": return "部分文件尚未交付"
        default: return "文件未能交付"
        }
    }
}
struct TaskFilesResponse: Decodable {
    var files: [OutputFile]
    var transfer_state: String
    var note: String?
    var retry_requested: Bool
}
struct FileRetryResponse: Decodable { var recorded: Bool; var node_online: Bool }

final class FileDisk {
    static let byteLimit = 16 * 1024 * 1024
    static let cacheLimit = 128 * 1024 * 1024
    let directory: URL
    init(directory: URL) { self.directory = directory.appendingPathComponent("files", isDirectory: true) }

    func url(for file: OutputFile, endpoint: String) throws -> URL {
        guard file.isValid, file.status == "ready" else { throw AppFailure.protocolMismatch }
        let endpointHash = ImageDisk.digest(Data(endpoint.utf8))
        return directory.appendingPathComponent(endpointHash, isDirectory: true)
            .appendingPathComponent(file.file_id + "-" + file.sha256, isDirectory: true)
            .appendingPathComponent(file.filename)
    }
    static func verify(_ url: URL, against file: OutputFile) throws {
        guard file.isValid, file.status == "ready",
              let size = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey]).fileSize,
              size == file.size_bytes, size <= byteLimit,
              try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else { throw AppFailure.protocolMismatch }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let chunk = try handle.read(upToCount: 64 * 1024), !chunk.isEmpty { hash.update(data: chunk) }
        let digest = hash.finalize().map { String(format: "%02x", $0) }.joined()
        guard digest == file.sha256 else { throw AppFailure.protocolMismatch }
    }
    func cached(_ file: OutputFile, endpoint: String) throws -> URL? {
        let target = try url(for: file, endpoint: endpoint)
        guard FileManager.default.fileExists(atPath: target.path) else { return nil }
        do { try Self.verify(target, against: file); return target }
        catch { try? FileManager.default.removeItem(at: target); return nil }
    }
    func install(_ downloaded: URL, file: OutputFile, endpoint: String) throws -> URL {
        try Self.verify(downloaded, against: file)
        let target = try url(for: file, endpoint: endpoint)
        let parent = target.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let temporary = parent.appendingPathComponent(UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: temporary) }
            try FileManager.default.copyItem(at: downloaded, to: temporary)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: target.path) { try FileManager.default.removeItem(at: target) }
            try FileManager.default.moveItem(at: temporary, to: target)
            // A filesystem copy can preserve the source date; eviction follows cache insertion time.
            try FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: target.path)
            prune(excluding: target)
            return target
        } catch { throw AppFailure.localSave }
    }
    func remove(_ file: OutputFile, endpoint: String) {
        if let target = try? url(for: file, endpoint: endpoint) { try? FileManager.default.removeItem(at: target) }
    }
    private func prune(excluding target: URL) {
        let keys: [URLResourceKey] = [.fileSizeKey, .isRegularFileKey, .contentModificationDateKey]
        guard let enumerator = FileManager.default.enumerator(at: directory, includingPropertiesForKeys: keys,
                                                              options: [.skipsHiddenFiles]) else { return }
        var entries: [(URL, Int, Date)] = []
        for case let url as URL in enumerator {
            guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true else { continue }
            entries.append((url, values.fileSize ?? 0, values.contentModificationDate ?? .distantPast))
        }
        var size = entries.reduce(0) { $0 + $1.1 }
        for entry in entries.sorted(by: { $0.2 < $1.2 }) where size > Self.cacheLimit && entry.0 != target {
            if (try? FileManager.default.removeItem(at: entry.0)) != nil { size -= entry.1 }
        }
    }
}
