"""Private, immutable image blobs. Only task associations grant node access."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat
import struct
import tempfile
import zlib

from hub.gateway import v1_repo as repo
from shared.v1_schemas import IMAGE_ID

IMAGE_MAX = 6 * 1024 * 1024
STORAGE_MAX = 512 * 1024 * 1024
DIMENSION_MAX = 4096
PIXEL_MAX = 16_000_000


def checked_id(image_id: str) -> str:
    if IMAGE_ID.fullmatch(image_id) is None:
        raise repo.RepoError(422, "invalid_image_id", "图片编号无效")
    return image_id


def invalid() -> None:
    raise repo.RepoError(422, "invalid_image", "图片格式或内容无效，请重新选择图片")


def dimensions(width: int, height: int) -> tuple[int, int]:
    if not (0 < width <= DIMENSION_MAX and 0 < height <= DIMENSION_MAX) or width * height > PIXEL_MAX:
        raise repo.RepoError(422, "image_dimensions", "图片尺寸过大，请缩小后再上传")
    return width, height


def png_dimensions(body: bytes) -> tuple[int, int]:
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        invalid()
    position, count, header, ended, idat_closed = 8, 0, None, False, False
    data_parts = []
    palette = False
    while position < len(body):
        count += 1
        if count > 10000 or position + 12 > len(body):
            invalid()
        length = struct.unpack_from(">I", body, position)[0]
        kind = body[position + 4:position + 8]
        end = position + 12 + length
        if end > len(body) or any(not (65 <= char <= 90 or 97 <= char <= 122) for char in kind):
            invalid()
        data = body[position + 8:end - 4]
        checksum = struct.unpack_from(">I", body, end - 4)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != checksum:
            invalid()
        if header is None and kind != b"IHDR":
            invalid()
        if kind == b"IHDR":
            if header is not None or length != 13:
                invalid()
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            dimensions(width, height)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(color, ()) or compression != 0 or filtering != 0 or interlace not in (0, 1):
                invalid()
            header = (width, height, depth, color, interlace)
        elif kind == b"PLTE":
            if palette or data_parts or not length or length % 3 or length > 768:
                invalid()
            palette = True
        elif kind == b"IDAT":
            if idat_closed:
                invalid()
            data_parts.append(data)
        elif kind == b"IEND":
            if length or not data_parts or end != len(body):
                invalid()
            ended = True
        elif kind in (b"acTL", b"fcTL", b"fdAT"):
            invalid()  # One static attachment, with a bounded decoded surface.
        elif kind[0] & 32 == 0:
            invalid()  # Unknown critical chunks cannot be treated as safely readable PNG.
        if data_parts and kind not in (b"IDAT", b"IEND"):
            idat_closed = True
        position = end
    if not ended or header is None:
        invalid()
    width, height, depth, color, interlace = header
    if color == 3 and not palette:
        invalid()
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = ((0, 0, 1, 1),) if not interlace else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    rows = []
    for x, y, dx, dy in passes:
        columns = max(0, (width - x + dx - 1) // dx)
        lines = max(0, (height - y + dy - 1) // dy)
        if columns and lines:
            rows.extend([(columns * channels * depth + 7) // 8] * lines)
    expected = sum(rows) + len(rows)
    decoded, row, remaining = 0, 0, 0
    decoder = zlib.decompressobj()
    try:
        for compressed in data_parts:
            while compressed:
                chunk = decoder.decompress(compressed, 65536)
                compressed = decoder.unconsumed_tail
                decoded += len(chunk)
                if decoded > expected or decoder.unused_data:
                    invalid()
                offset = 0
                while offset < len(chunk):
                    if not remaining:
                        if row >= len(rows) or chunk[offset] > 4:
                            invalid()
                        remaining = rows[row]; row += 1; offset += 1
                    amount = min(remaining, len(chunk) - offset)
                    offset += amount; remaining -= amount
        if not decoder.eof or decoder.unused_data or decoded != expected or remaining or row != len(rows):
            invalid()
    except zlib.error:
        invalid()
    return width, height


def jpeg_dimensions(body: bytes) -> tuple[int, int]:
    # Standard-library structural validation: marker bounds, supported frame and complete scan/EOI.
    # Entropy decoding belongs to the platform image decoder; never trust EXIF dimensions.
    if not body.startswith(b"\xff\xd8"):
        invalid()
    position, markers, frame, scans = 2, 0, None, 0
    components = set()
    while position < len(body):
        markers += 1
        if markers > 10000 or body[position] != 0xFF:
            invalid()
        while position < len(body) and body[position] == 0xFF:
            position += 1
        if position == len(body):
            invalid()
        marker = body[position]; position += 1
        if marker == 0xD9:
            if frame is None or not scans or position != len(body):
                invalid()
            return frame
        if marker in (0, 0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or position + 2 > len(body):
            invalid()
        length = struct.unpack_from(">H", body, position)[0]
        if length < 2 or position + length > len(body):
            invalid()
        data = body[position + 2:position + length]
        position += length
        if marker in (0xC0, 0xC1, 0xC2):
            if frame is not None or len(data) < 6:
                invalid()
            precision, height, width, count = struct.unpack_from(">BHHB", data)
            if precision != 8 or count not in (1, 3, 4) or len(data) != 6 + 3 * count:
                invalid()
            components = {data[6 + 3 * n] for n in range(count)}
            if len(components) != count or any(not (1 <= data[7 + 3 * n] >> 4 <= 4 and 1 <= data[7 + 3 * n] & 15 <= 4) for n in range(count)):
                invalid()
            frame = dimensions(width, height)
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            invalid()
        elif marker == 0xDA:
            if frame is None or not data:
                invalid()
            count = data[0]
            if not count or count > len(components) or len(data) != 1 + 2 * count + 3:
                invalid()
            selected = {data[1 + 2 * n] for n in range(count)}
            if len(selected) != count or not selected <= components:
                invalid()
            scans += 1
            # Entropy bytes may contain stuffed FF00 and restart markers, but must end at a marker.
            start = position
            while position < len(body):
                if body[position] != 0xFF:
                    position += 1; continue
                cursor = position + 1
                while cursor < len(body) and body[cursor] == 0xFF:
                    cursor += 1
                if cursor == len(body):
                    invalid()
                if body[cursor] == 0 or 0xD0 <= body[cursor] <= 0xD7:
                    position = cursor + 1
                else:
                    break
            if position == start or position == len(body):
                invalid()
    invalid()


def inspect(body: bytes, mime_type: str) -> tuple[int, int]:
    if not body or len(body) > IMAGE_MAX:
        raise repo.RepoError(413, "image_too_large", "图片超过 6 MiB 或为空，请重新选择")
    if mime_type == "image/png":
        return png_dimensions(body)
    if mime_type == "image/jpeg":
        return jpeg_dimensions(body)
    raise repo.RepoError(415, "unsupported_image", "仅支持 JPEG 或 PNG 图片")


class ImageStore:
    def __init__(self, db_path: str, max_bytes: int = STORAGE_MAX):
        self.directory = Path(db_path).resolve().parent / "images"
        if self.directory.is_symlink():
            raise ValueError("图片目录不能为符号链接")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def read(self, image_id: str, metadata: dict) -> bytes:
        path = self.directory / checked_id(image_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise OSError("not_regular")
                body = handle.read(IMAGE_MAX + 1)
        except OSError:
            raise repo.RepoError(503, "image_unavailable", "图片暂时无法读取，请稍后重试") from None
        if len(body) != metadata["size_bytes"] or hashlib.sha256(body).hexdigest() != metadata["sha256"]:
            raise repo.RepoError(503, "image_unavailable", "图片完整性核对失败，请稍后重试")
        return body

    def put(self, conn, image_id: str, body: bytes, mime_type: str, task: dict | None = None) -> dict:
        checked_id(image_id)
        width, height = inspect(body, mime_type)
        digest = hashlib.sha256(body).hexdigest()
        existing = conn.execute("SELECT * FROM v1_images WHERE image_id=?", (image_id,)).fetchone()
        association = None
        if task:
            association = conn.execute("SELECT direction FROM v1_task_images WHERE task_id=? AND image_id=?", (task["task_id"], image_id)).fetchone()
            if existing and association is None:
                raise repo.RepoError(403, "forbidden_role", "节点不能引用未关联本任务的既有图片")
            if association and association["direction"] != "output":
                raise repo.RepoError(409, "image_conflict", "任务输入图片不可作为结果覆盖")
            if not association and task["status"] not in {*repo.ACTIVE, "done"}:
                raise repo.RepoError(409, "invalid_state", "此任务当前不能接收结果图片")
            if not association and conn.execute("SELECT COUNT(*) FROM v1_task_images WHERE task_id=? AND direction='output'", (task["task_id"],)).fetchone()[0] >= 4:
                raise repo.RepoError(422, "image_limit", "每轮最多接收 4 张结果图片")
        if existing:
            if existing["sha256"] != digest or existing["mime_type"] != mime_type:
                raise repo.RepoError(409, "image_conflict", "此图片编号已用于不同内容")
            # Missing or damaged disk bytes may be safely repaired by the same immutable upload.
            try:
                self.read(image_id, existing)
                return repo.image_view(existing)
            except repo.RepoError:
                pass
        path = self.directory / image_id
        newly_written = not path.exists()
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise repo.RepoError(503, "image_unavailable", "图片存储位置不可用")
        if path.exists() and existing is None:
            if path.stat().st_size > IMAGE_MAX:
                raise repo.RepoError(409, "image_conflict", "此图片编号已用于不同内容")
            orphan = path.read_bytes()
            if hashlib.sha256(orphan).hexdigest() != digest:
                raise repo.RepoError(409, "image_conflict", "此图片编号已用于不同内容")
        used = sum(entry.stat(follow_symlinks=False).st_size for entry in os.scandir(self.directory) if entry.is_file(follow_symlinks=False))
        replaced_size = path.stat().st_size if path.exists() else 0
        if used - replaced_size + len(body) > self.max_bytes:
            raise repo.RepoError(507, "image_storage_full", "图片存储已满，请在中枢检查空间")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".upload-", dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with conn:
                if existing is None:
                    conn.execute("INSERT INTO v1_images(image_id,mime_type,size_bytes,sha256,width,height,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (image_id, mime_type, len(body), digest, width, height, repo.now_iso()))
                if task and association is None:
                    position = conn.execute("SELECT COALESCE(MAX(position)+1,0) FROM v1_task_images WHERE task_id=? AND direction='output'", (task["task_id"],)).fetchone()[0]
                    conn.execute("INSERT INTO v1_task_images(task_id,image_id,direction,position) VALUES(?,?,'output',?)", (task["task_id"], image_id, position))
        except Exception as exc:
            if newly_written:
                path.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise repo.RepoError(507, "image_storage_error", "图片保存失败，请在中枢检查空间后重试") from None
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return repo.image_view(conn.execute("SELECT * FROM v1_images WHERE image_id=?", (image_id,)).fetchone())
