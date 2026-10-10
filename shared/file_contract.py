"""Bounded output-file metadata and container checks shared by Hub and Node."""
from __future__ import annotations
import hashlib
import io
import re
import stat
import zipfile
import unicodedata
from pathlib import PurePosixPath
from xml.etree import ElementTree

FILE_ID = re.compile(r"f-[0-9a-f]{32}\Z")
MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 4
STORAGE_MAX = 512 * 1024 * 1024
TYPES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
         ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
FIELDS = ("file_id", "filename", "mime_type", "size_bytes", "sha256")


class FileError(ValueError):
    """Safe generic message; never retains URLs, paths, bodies or credentials."""


def filename(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in "/\\"):
        raise FileError("文件名称无效")
    try:
        if len(value.encode("utf-8")) > 180 or any(unicodedata.category(c) == "Cc" for c in value):
            raise FileError("文件名称过长或包含控制字符")
    except UnicodeEncodeError:
        raise FileError("文件名称无效") from None
    if PurePosixPath(value).suffix.lower() not in TYPES:
        raise FileError("仅支持 TXT、Markdown、CSV、PDF 或 DOCX 文件")
    return value


def metadata(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("file_id"), str) or FILE_ID.fullmatch(value["file_id"]) is None:
        raise FileError("文件编号无效")
    name = filename(value.get("filename"))
    if value.get("mime_type") != TYPES[PurePosixPath(name).suffix.lower()]:
        raise FileError("文件类型与名称不匹配")
    if type(value.get("size_bytes")) is not int or not 0 < value["size_bytes"] <= MAX_BYTES:
        raise FileError("文件为空或超过 16 MiB")
    if not isinstance(value.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None:
        raise FileError("文件校验信息无效")
    return {key: value[key] for key in FIELDS}


def inspect(body: bytes, mime: str) -> None:
    if not 0 < len(body) <= MAX_BYTES:
        raise FileError("文件为空或超过 16 MiB")
    if mime in {"text/plain", "text/markdown", "text/csv"}:
        try:
            body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FileError("文本文件必须使用 UTF-8 编码") from None
        if b"\0" in body:
            raise FileError("文本文件包含无效内容")
    elif mime == "application/pdf":
        if re.match(rb"%PDF-(?:1\.[0-7]|2\.0)(?:\r\n|\r|\n)", body) is None or not body.rstrip().endswith(b"%%EOF"):
            raise FileError("PDF 文件容器不完整")
    elif mime == TYPES[".docx"]:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries = archive.infolist()
                names = {entry.filename for entry in entries}
                required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
                if not 0 < len(entries) <= 2000 or len(names) != len(entries) or not required <= names or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
                    raise FileError("DOCX 成员或解压大小无效")
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    mode = entry.external_attr >> 16
                    if (entry.flag_bits & 1 or path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                        or re.match(r"^[A-Za-z]:", entry.filename) or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or stat.S_ISLNK(mode) or any(ord(c) < 32 for c in entry.filename)
                        or "vba" in entry.filename.lower() or entry.filename.lower().endswith(".bin")):
                        raise FileError("DOCX 包含不支持的成员")
                    # Read every member to verify CRC without retaining the expanded archive.
                    with archive.open(entry) as stream:
                        read = 0
                        while chunk := stream.read(65536):
                            read += len(chunk)
                            if read > entry.file_size:
                                raise FileError("DOCX 成员大小无效")
                for name in required:
                    entry = archive.getinfo(name)
                    if entry.file_size > 2 * 1024 * 1024:
                        raise FileError("DOCX 必需 XML 成员过大")
                    xml = archive.read(name)
                    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                        raise FileError("DOCX XML 不支持实体声明")
                    ElementTree.fromstring(xml)
                types = archive.read("[Content_Types].xml")
                if b"macroenabled" in types.lower() or b"vbaproject" in types.lower():
                    raise FileError("不支持带宏的 DOCX")
                if b"wordprocessingml.document.main+xml" not in types:
                    raise FileError("DOCX 文档类型无效")
        except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, NotImplementedError, EOFError, ElementTree.ParseError) as exc:
            if isinstance(exc, FileError):
                raise
            raise FileError("DOCX 文件容器不完整") from None
    else:
        raise FileError("文件类型不支持")


def describe(body: bytes, file_id: str, name: str) -> dict:
    filename(name)
    mime = TYPES[PurePosixPath(name).suffix.lower()]
    inspect(body, mime)
    return metadata({"file_id": file_id, "filename": name, "mime_type": mime,
                     "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
