"""Explicit final-answer output files, frozen bytes and no-model retry worker."""
from __future__ import annotations
import asyncio
import hashlib
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit
import httpx

from node.dsh_images import image_url, private_directory, private_write
from shared.file_contract import FILE_ID, FIELDS, MAX_BYTES, MAX_FILES, FileError, describe, metadata


def file_url(cfg: dict, task_id: str, suffix: str = "") -> str:
    # Reuse the audited HTTPS and reverse-proxy-prefix checks, without accepting arbitrary URLs.
    route = image_url(cfg, task_id, "i-" + "0" * 32).rsplit("/images/", 1)[0] + "/files"
    if suffix and suffix not in {"manifest", "retry"} and FILE_ID.fullmatch(suffix) is None:
        raise FileError("文件编号无效")
    return route + ("/" + suffix if suffix else "")


def file_prompt(prompt: str) -> str:
    return prompt + ("\n\n[成果文件交付约定]\n仅在任务确有文件成果时，将 TXT/Markdown/CSV/PDF/DOCX 保存到当前工作区内，"
        "在最终回答用普通 Markdown 链接 [名称](相对路径) 明确声明；最多四件、每件最多 16 MiB。"
        "图片继续使用图片链接。不要只给外部 URL 或本机绝对路径作为交付。"
        "最终回答按实际情况说明做了什么、成果、已做的检查及剩余问题；未做的检查不能声称通过。\n")


def markdown_files(text: str) -> list[str]:
    lines, fence, kept = text.splitlines(keepends=True), None, []
    for line in lines:
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line[match.end():].strip():
                fence = None
            continue
        if match:
            fence = match[1]
            continue
        kept.append(line)
    text = re.sub(r"(`+)([^`]*?)\1", "", "".join(kept))
    definitions = {}
    key = lambda label: re.sub(r"\s+", " ", label.strip()).casefold()
    definition = r"(?m)^ {0,3}\[([^\]\n]+)\]:\s*(?:<([^>\n]+)>|(\S+))[^\n]*(?:\n|$)"
    for match in re.finditer(definition, text):
        definitions.setdefault(key(match[1]), match[2] or match[3])
    text = re.sub(definition, "", text)
    result = []
    pattern = r"(?<!!)\[(?P<label>(?:\\.|[^\]\\])*)\](?:(?P<inline>\(\s*)|\[(?P<ref>[^\]\n]*)\]|(?![([]))"
    for match in re.finditer(pattern, text):
        count, before = 0, match.start() - 1
        while before >= 0 and text[before] == "\\":
            count, before = count + 1, before - 1
        if count % 2:
            continue
        if match.group("inline") is None:
            value = definitions.get(key(match.group("ref") or match.group("label")))
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
                    pos += 2; continue
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
        if value and re.match(r'''\s*(?:"[^"\n]*"|'[^'\n]*'|\([^\n]*?\))?\s*\)''', text[pos:]):
            result.append(re.sub(r"\\([\\ ()<>])", r"\1", value))
    return result


def local_file(cwd: str, reference: str) -> str | None:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        return None  # Ordinary external documentation links are not delivery failures.
    if parsed.query or parsed.fragment:
        raise FileError("成果文件路径不能包含查询或片段")
    value = unquote(parsed.path)
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise FileError("成果文件路径无效")
    root = Path(cwd).resolve(strict=True)
    candidate = Path(value) if Path(value).is_absolute() else root / value
    if ".." in candidate.parts:
        raise FileError("成果文件路径不可跨越目录")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise FileError("成果文件不在任务工作区内")
    current = candidate
    while current != current.parent:
        if current.is_symlink():
            raise FileError("成果文件路径不可经过符号链接")
        if current == root:
            break
        current = current.parent
    return resolved.relative_to(root).as_posix()


def read_regular(path, *, dir_fd=None) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise FileError("成果文件为空、过大或不是普通文件")
        body = stream.read(MAX_BYTES + 1)
    if not 0 < len(body) <= MAX_BYTES:
        raise FileError("成果文件为空或超过 16 MiB")
    return body


def read_workspace(cwd: str, relative: str, identity=None) -> bytes:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {".", ".."} for p in parts):
        raise FileError("成果文件路径无效")
    descriptor = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if identity and [info.st_dev, info.st_ino] != identity:
            raise FileError("任务工作区身份已改变")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        return read_regular(parts[-1], dir_fd=descriptor)
    finally:
        os.close(descriptor)


def collect_outputs(cfg: dict, journal, task_id: str, text: str):
    task = journal.get(task_id)
    if "output_files" in task:
        return
    files, notes, seen = [], [], set()
    for reference in markdown_files(text):
        try:
            relative = local_file(task["cwd"], reference)
            if relative is None or relative in seen:
                continue
            # Ignore explicit image links written with ordinary-link syntax; images have their own contract.
            if Path(relative).suffix.lower() in {".png", ".jpg", ".jpeg"}:
                continue
            seen.add(relative)
            if len(files) >= MAX_FILES:
                notes.append("最多传回四件成果文件"); break
            body = read_workspace(task["cwd"], relative, task.get("project_identity"))
            file_id = "f-" + hashlib.sha256((task_id + "\0" + relative).encode()).hexdigest()[:32]
            value = describe(body, file_id, Path(relative).name)
            directory = private_directory(cfg, task_id, "output-files")
            private_write(directory / file_id, body)
            files.append({**value, "status": "pending", "error": None})
        except (FileError, OSError, ValueError):
            notes.append("有成果文件无法交付：路径、格式或文件不符合要求")
    journal.update(task_id, output_files=files, file_collection_note="；".join(dict.fromkeys(notes)),
                   files_note="；".join(dict.fromkeys(notes)), file_manifest_uploaded=False)


def manifest(task: dict) -> dict:
    return {"files": [{**metadata(value), "status": "failed" if value.get("error") else "pending", "error": value.get("error")} for value in task.get("output_files", [])],
            "note": task.get("file_collection_note", "")}


async def upload_outputs(cfg: dict, journal, task_id: str) -> str:
    """POST fixed manifest and retry private copies; this path never creates an execution."""
    task = journal.get(task_id)
    if not task or task.get("status") != "done" or "output_files" not in task:
        return ""
    files = task["output_files"]
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(15, connect=5),
                                 headers={"Authorization": "Bearer " + cfg["node_token"]}) as client:
        try:
            response = await client.post(file_url(cfg, task_id, "manifest"), json=manifest(task))
            if response.status_code != 200:
                raise FileError("成果文件登记暂未完成")
            actual = response.json().get("files")
            if not isinstance(actual, list) or [metadata(v) for v in actual] != [metadata(v) for v in files]:
                raise FileError("成果文件登记与本机记录不一致")
            journal.update(task_id, file_manifest_uploaded=True)
        except (FileError, httpx.HTTPError, OSError, ValueError):
            note = "成果文件暂未登记，将在重新连接后补传"
            journal.update(task_id, files_note=note, file_manifest_uploaded=False)
            return note
        for value in files:
            if value.get("status") == "uploaded":
                continue
            try:
                directory = private_directory(cfg, task_id, "output-files")
                body = read_regular(directory / value["file_id"])
                if describe(body, value["file_id"], value["filename"]) != metadata(value):
                    raise FileError("待补传成果文件校验失败")
                response = await asyncio.wait_for(client.put(file_url(cfg, task_id, value["file_id"]), content=body,
                    headers={"Content-Type": value["mime_type"]}), timeout=20)
                actual = response.json() if response.status_code == 200 else {}
                if metadata(actual) != metadata(value) or actual.get("status") != "ready":
                    raise FileError("成果文件上传未确认")
                value.update(status="uploaded", error=None)
            except (FileError, httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError):
                value.update(status="failed", error="成果文件暂未传回，将在重新连接后补传")
            journal.update(task_id, output_files=files)
        # Publish failures independently, without changing the completed task verdict.
        try:
            response = await client.post(file_url(cfg, task_id, "manifest"), json=manifest(journal.get(task_id)))
            if response.status_code != 200:
                journal.update(task_id, file_manifest_uploaded=False)
        except (httpx.HTTPError, FileError, ValueError):
            journal.update(task_id, file_manifest_uploaded=False)
    note = "；".join(dict.fromkeys([task.get("file_collection_note", "")] + [v["error"] for v in files if v.get("error")])).strip("；")
    journal.update(task_id, files_note=note)
    return note
