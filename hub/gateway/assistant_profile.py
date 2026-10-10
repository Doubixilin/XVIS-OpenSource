"""用户明确编辑的共享助理资料；任务只使用创建时的固定快照。"""
from __future__ import annotations
import json

from hub.gateway.v1_repo import RepoError, dump
from shared.v1_schemas import AssistantProfile


def read_profile(conn) -> dict:
    row = conn.execute("SELECT revision,profile_json FROM v1_assistant_profile WHERE singleton=1").fetchone()
    return {"revision": row["revision"], **json.loads(row["profile_json"])}


def write_profile(conn, profile: AssistantProfile) -> dict:
    data = profile.model_dump()
    revision = data.pop("revision")
    # The conditional write also protects another Hub connection's accepted edit.
    with conn:
        updated = conn.execute("UPDATE v1_assistant_profile SET revision=revision+1,profile_json=? WHERE singleton=1 AND revision=?",
                               (dump(data), revision))
        if not updated.rowcount:
            raise RepoError(409, "profile_conflict", "助理资料已更新，请保留当前编辑稿并重新载入后再保存")
    return {"revision": revision + 1, **data}


def task_snapshot(conn) -> dict:
    profile = read_profile(conn)
    return {"name": profile["name"],
            "persona": profile["persona"] if profile["persona_enabled"] else "",
            "memories": [entry["text"] for entry in profile["memories"] if entry["enabled"]]
            if profile["memory_enabled"] else []}
