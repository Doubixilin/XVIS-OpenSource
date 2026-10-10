"""Bounded, authenticated task images; no model calls or workspace scanning."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import struct
import zlib
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

MAX_IMAGES = 4
MAX_BYTES = 6 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 16_000_000
IMAGE_ID = re.compile(r"i-[0-9a-f]{32}\Z")
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg"}


class ImageError(ValueError):
    """A safe-to-display failure, without response bodies, URLs or credentials."""


def metadata(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("image_id"), str) or not IMAGE_ID.fullmatch(value["image_id"]):
        raise ImageError("图片编号无效")
    if value.get("mime_type") not in EXTENSIONS or not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ImageError("图片类型或校验信息无效")
    for key, limit in (("size_bytes", MAX_BYTES), ("width", MAX_DIMENSION), ("height", MAX_DIMENSION)):
        if type(value.get(key)) is not int or not 0 < value[key] <= limit:
            raise ImageError("图片大小或尺寸超出限制")
    if value["width"] * value["height"] > MAX_PIXELS:
        raise ImageError("图片像素超出限制")
    return {k: value[k] for k in ("image_id", "mime_type", "size_bytes", "sha256", "width", "height")}


def input_metadata(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_IMAGES:
        raise ImageError("一次最多发送四张图片")
    result = [metadata(v) for v in value]
    if len({v["image_id"] for v in result}) != len(result):
        raise ImageError("图片编号重复")
    return result


def image_info(data: bytes) -> tuple[str, int, int]:
    """Validate the bounded image container and dimensions; Hub performs admission too."""
    if not 0 < len(data) <= MAX_BYTES:
        raise ImageError("图片超过 6 MiB 或为空")
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos, first, ended = 8, True, False
        while pos + 12 <= len(data):
            size = int.from_bytes(data[pos:pos + 4], "big")
            end = pos + 12 + size
            if end > len(data):
                raise ImageError("PNG 图片不完整")
            kind, content = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + size]
            if zlib.crc32(kind + content) & 0xffffffff != int.from_bytes(data[end - 4:end], "big"):
                raise ImageError("PNG 图片校验失败")
            if first:
                if kind != b"IHDR" or size != 13:
                    raise ImageError("PNG 图片头无效")
                width, height = struct.unpack(">II", content[:8])
                first = False
            if kind == b"IEND":
                ended = size == 0 and end == len(data)
                break
            pos = end
        if not ended:
            raise ImageError("PNG 图片不完整")
        mime = "image/png"
    elif data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        pos = 2
        while pos < len(data) - 2:
            if data[pos] != 0xff:
                raise ImageError("JPEG 图片头无效")
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker in (0xd8, 0xd9, 0x01) or 0xd0 <= marker <= 0xd7:
                continue
            if pos + 2 > len(data):
                break
            size = int.from_bytes(data[pos:pos + 2], "big")
            if size < 2 or pos + size > len(data):
                raise ImageError("JPEG 图片不完整")
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if size < 8:
                    raise ImageError("JPEG 尺寸信息无效")
                height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            if marker == 0xda:
                break
            pos += size
        mime = "image/jpeg"
    else:
        raise ImageError("仅支持完整的 PNG / JPEG 图片")
    if not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or width * height > MAX_PIXELS:
        raise ImageError("图片尺寸超出限制")
    return mime, width, height


def describe(data: bytes, image_id: str) -> dict:
    mime, width, height = image_info(data)
    return metadata({"image_id": image_id, "mime_type": mime, "size_bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height})


def image_url(cfg: dict, task_id: str, image_id: str, content: bool = False) -> str:
    if not TASK_ID.fullmatch(task_id) or not IMAGE_ID.fullmatch(image_id):
        raise ImageError("任务或图片编号无效")
    parsed = urlsplit(cfg.get("hub_ws_url", ""))
    suffix = "/ws/node"
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or not parsed.path.endswith(suffix):
        raise ImageError("中枢图片地址配置无效")
    prefix = parsed.path[:-len(suffix)]
    for segment in prefix.split("/")[1:]:
        decoded = unquote(segment)
        if not decoded or decoded in (".", "..") or any(c in decoded for c in ("/", "\\")) or any(ord(c) < 32 for c in decoded):
            raise ImageError("中枢图片地址前缀无效")
    if parsed.scheme == "wss":
        scheme = "https"
    elif parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        scheme = "http"
    else:
        raise ImageError("中枢图片传输必须使用 HTTPS")
    if not parsed.hostname:
        raise ImageError("中枢图片地址配置无效")
    route = prefix + f"/api/v1/tasks/{task_id}/images/{image_id}" + ("/content" if content else "")
    return urlunsplit((scheme, parsed.netloc, route, "", ""))


def private_directory(cfg: dict, task_id: str, name: str) -> Path:
    if not TASK_ID.fullmatch(task_id):
        raise ImageError("任务编号无效")
    root = Path(cfg["log_dir"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (root, root / task_id, root / task_id / name):
        if directory.is_symlink():
            raise ImageError("任务图片目录不可为符号链接")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return root / task_id / name


def private_write(path: Path, data: bytes):
    temporary = path.with_name(path.name + ".part")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def read_regular(path: Path, *, dir_fd=None) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        import stat
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise ImageError("图片文件为空、过大或不是普通文件")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ImageError("图片文件超过 6 MiB")
    return data


def read_workspace_image(cwd: str, relative: str, identity=None) -> bytes:
    """Walk with directory FDs so a concurrent symlink swap cannot escape the workspace."""
    parts = Path(relative).parts
    if not parts or any(part in (".", "..") for part in parts) or Path(relative).is_absolute():
        raise ImageError("结果图片路径无效")
    fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if identity and [info.st_dev, info.st_ino] != identity:
            raise ImageError("当前工作区与任务记录不一致")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return read_regular(Path(parts[-1]), dir_fd=fd)
    finally:
        os.close(fd)


async def download_inputs(cfg: dict, task: dict) -> list[Path]:
    images = input_metadata(task.get("images", []))
    if not images:
        return []
    directory = private_directory(cfg, task["task_id"], "input-images")
    paths = []
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(12, connect=5),
                                 headers={"Authorization": "Bearer " + cfg["node_token"]}) as client:
        for image in images:
            path = directory / (image["image_id"] + EXTENSIONS[image["mime_type"]])
            async with client.stream("GET", image_url(cfg, task["task_id"], image["image_id"], True)) as response:
                if response.status_code != 200:
                    raise ImageError(f"输入图片下载失败（HTTP {response.status_code}）")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != image["mime_type"]:
                    raise ImageError("输入图片类型与记录不一致")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > image["size_bytes"] or len(data) + len(chunk) > MAX_BYTES:
                        raise ImageError("输入图片下载大小与记录不一致")
                    data.extend(chunk)
            payload = bytes(data)
            if describe(payload, image["image_id"]) != image:
                raise ImageError("输入图片内容与校验记录不一致")
            private_write(path, payload)
            paths.append(path.resolve())
    return paths


def image_prompt(original: str, paths: list[Path]) -> str:
    output_instruction = ("\n\n[结果图片回传约定]\n若本任务产出需要传回的图片，请保存为当前工作区内的 PNG/JPEG，"
                          "在最终回答使用 ![说明](相对路径) 显式引用。此约定不要求无关任务生成图片。\n")
    if not paths:
        return original + output_instruction
    return original + "\n\n[本轮用户上传的图片]\n" + "\n".join(
        f"{index}. {path}" for index, path in enumerate(paths, 1)) + (
        "\n请先逐张调用官方 read_image 工具读取以上图片，再处理用户原指令。"
        "路径或文件名不等于图片内容；若 read_image 失败或当前模型不接受图像，"
        "请明确说明无法查看图片，不要假装已经看图，也不要用 OCR 冒充视觉理解。\n") + output_instruction


def markdown_images(text: str) -> list[str]:
    # Code examples are not output declarations. Preserve the remaining text order.
    text = re.sub(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[^\n]*(?:\n|$)", "", text)
    text = re.sub(r"(`+)([^`]*?)\1", "", text)
    definitions = {}
    def label_key(label):
        return re.sub(r"\s+", " ", label.strip()).casefold()
    for definition in re.finditer(r"(?m)^ {0,3}\[([^\]\n]+)\]:\s*(?:<([^>\n]+)>|(\S+))", text):
        definitions.setdefault(label_key(definition.group(1)), definition.group(2) or definition.group(3))
    result = []
    pattern = r"!\[(?P<label>(?:\\.|[^\]\\])*)\](?:(?P<inline>\(\s*)|\[(?P<ref>[^\]\n]*)\]|(?![([]))"
    for match in re.finditer(pattern, text):
        slash_count, before = 0, match.start() - 1
        while before >= 0 and text[before] == "\\":
            slash_count, before = slash_count + 1, before - 1
        if slash_count % 2:
            continue
        if match.group("inline") is None:
            value = definitions.get(label_key(match.group("ref") or match.group("label")))
            if value:
                result.append(value)
            continue
        start = pos = match.end()
        if pos < len(text) and text[pos] == "<":
            end = text.find(">", pos + 1)
            if end < 0:
                continue
            value, pos = text[pos + 1:end], end + 1
        else:
            depth = 0
            while pos < len(text):
                char = text[pos]
                if char == "\\" and pos + 1 < len(text):
                    pos += 2
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace() and depth == 0:
                    break
                pos += 1
            value = text[start:pos]
        remainder = text[pos:]
        if not re.match(r'''\s*(?:"[^"\n]*"|'[^'\n]*'|\([^\n]*?\))?\s*\)''', remainder):
            continue
        if value:
            result.append(re.sub(r"\\([\\ ()<>])", r"\1", value))
    return result


def local_image(cwd: str, reference: str) -> tuple[Path, str]:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ImageError("结果图片只支持当前工作区内的本地文件")
    value = unquote(parsed.path)
    if not value or "\x00" in value:
        raise ImageError("结果图片路径无效")
    root = Path(cwd).resolve(strict=True)
    candidate = Path(value) if Path(value).is_absolute() else root / value
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ImageError("结果图片不在当前工作区内")
    # Reject any symlink traversal, including paths which currently point back inside.
    current = candidate
    while current != current.parent:
        if current.is_symlink():
            raise ImageError("结果图片路径不可经过符号链接")
        if current == root:
            break
        current = current.parent
    if resolved.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ImageError("结果图片仅支持 PNG / JPEG")
    return resolved, resolved.relative_to(root).as_posix()


def collect_outputs(cfg: dict, journal, task_id: str, text: str):
    task = journal.get(task_id)
    if "output_images" in task:
        return
    records, notes, seen = [], [], set()
    for reference in markdown_images(text):
        if len(records) >= MAX_IMAGES:
            notes.append("最多传回四张结果图片")
            break
        try:
            _, relative = local_image(task["cwd"], reference)
            if relative in seen:
                continue
            seen.add(relative)
            data = read_workspace_image(task["cwd"], relative, task.get("project_identity"))
            image_id = "i-" + hashlib.sha256((task_id + "\0" + relative).encode()).hexdigest()[:32]
            record = describe(data, image_id)
            directory = private_directory(cfg, task_id, "output-images")
            private_write(directory / (image_id + EXTENSIONS[record["mime_type"]]), data)
            records.append({**record, "relative_path": relative, "status": "pending"})
        except (ImageError, OSError, ValueError):
            notes.append("有结果图片无法传回：路径、格式或文件不符合要求")
    journal.update(task_id, output_images=records, image_output_notes=list(dict.fromkeys(notes)),
                   images_note="；".join(dict.fromkeys(notes)))


async def upload_outputs(cfg: dict, journal, task_id: str) -> str:
    """Retry immutable staged outputs only; never invoke or resume a model."""
    task = journal.get(task_id)
    if task is None or task.get("status") != "done":
        return ""
    records = task.get("output_images", [])
    if not records or all(record.get("status") == "uploaded" for record in records):
        return task.get("images_note", "")
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(12, connect=5),
                                 headers={"Authorization": "Bearer " + cfg["node_token"]}) as client:
        for record in records:
            if record.get("status") == "uploaded":
                continue
            try:
                expected = metadata(record)
                directory = private_directory(cfg, task_id, "output-images")
                data = read_regular(directory / (record["image_id"] + EXTENSIONS[record["mime_type"]]))
                if describe(data, record["image_id"]) != expected:
                    raise ImageError("待补传图片校验失败")
                response = await asyncio.wait_for(client.put(image_url(cfg, task_id, record["image_id"]),
                    content=data, headers={"Content-Type": record["mime_type"]}), timeout=15)
                if response.status_code not in (200, 201):
                    raise ImageError(f"结果图片上传失败（HTTP {response.status_code}）")
                if metadata(response.json()) != expected:
                    raise ImageError("结果图片上传确认与本机记录不一致")
                record["status"] = "uploaded"
                record.pop("error", None)
            except (httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError):
                record["error"] = "结果图片暂未传回，将在重新连接后补传"
            journal.update(task_id, output_images=records)
    notes = task.get("image_output_notes", []) + [record["error"] for record in records if record.get("error")]
    note = "；".join(dict.fromkeys(notes))
    journal.update(task_id, images_note=note)
    return note
