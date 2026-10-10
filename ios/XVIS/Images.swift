import Foundation
import CryptoKit

struct ImageView: Codable, Identifiable, Equatable {
    var image_id: String
    var mime_type: String
    var size_bytes: Int
    var sha256: String
    var width: Int
    var height: Int
    var direction: String?
    var id: String { image_id }
}

struct PhotoDraft: Codable, Identifiable, Equatable {
    var imageID: String
    var fileName: String
    var id: String { imageID }
}

final class ImageDisk {
    static let byteLimit = 6 * 1024 * 1024
    let directory: URL
    init(directory: URL) { self.directory = directory.appendingPathComponent("images", isDirectory: true) }

    static func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    static func contentType(_ data: Data) throws -> String {
        if data.starts(with: [137, 80, 78, 71, 13, 10, 26, 10]) { return "image/png" }
        if data.starts(with: [255, 216, 255]) { return "image/jpeg" }
        throw AppFailure.network("这张图片的格式暂不支持，请选择照片或截图。")
    }
    private func url(_ name: String) throws -> URL {
        guard name.range(of: "^i-[0-9a-f]{32}(-[0-9a-f]{64})?\\.(jpg|png)$", options: .regularExpression) != nil else {
            throw AppFailure.protocolMismatch
        }
        return directory.appendingPathComponent(name)
    }
    func write(_ data: Data, named name: String) throws {
        guard !data.isEmpty, data.count <= Self.byteLimit else { throw AppFailure.network("单张图片不能超过 6 MB。") }
        let target = try url(name)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try data.write(to: target, options: [.atomic, .completeFileProtection])
        } catch { throw AppFailure.localSave }
    }
    func read(_ name: String) throws -> Data { try Data(contentsOf: url(name)) }
    func draft(_ data: Data) throws -> PhotoDraft {
        let mime = try Self.contentType(data)
        let id = "i-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let name = id + (mime == "image/png" ? ".png" : ".jpg")
        try write(data, named: name)
        return PhotoDraft(imageID: id, fileName: name)
    }
    static func cachedName(_ image: ImageView) throws -> String {
        guard image.image_id.range(of: "^i-[0-9a-f]{32}$", options: .regularExpression) != nil,
              image.sha256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              ["image/png", "image/jpeg"].contains(image.mime_type), image.size_bytes > 0,
              image.size_bytes <= Self.byteLimit, image.width > 0, image.height > 0,
              image.width <= 4096, image.height <= 4096 else { throw AppFailure.protocolMismatch }
        return image.image_id + "-" + image.sha256 + (image.mime_type == "image/png" ? ".png" : ".jpg")
    }
    static func verify(_ data: Data, against image: ImageView) throws {
        _ = try cachedName(image)
        guard data.count == image.size_bytes, Self.digest(data) == image.sha256,
              try Self.contentType(data) == image.mime_type else { throw AppFailure.protocolMismatch }
    }
}
