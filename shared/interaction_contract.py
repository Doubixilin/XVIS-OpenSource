"""Bounded H1-02 data shared by the Hub and local Host adapter."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

OPEN = {"pending", "submitted"}
CLOSED = {"confirmed", "cancelled", "expired", "interrupted"}


def string(value, limit=2048):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError("交互文字为空或超出限制")
    return value


def deadline(value):
    string(value, 64)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("交互时限必须含时区")
    return result


def expired(value):
    return deadline(value) <= datetime.now(timezone.utc)


def request(value):
    if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False).encode()) > 24576:
        raise ValueError("交互请求过大")
    if not re.fullmatch(r"q-[0-9a-f]{32}", string(value.get("interaction_id"), 34)):
        raise ValueError("交互编号无效")
    if type(value.get("revision")) is not int or value["revision"] != 1:
        raise ValueError("交互版本无效")
    for key in ("epoch", "session_id", "call_id"):
        string(value.get(key), 256)
    deadline(value.get("expires_at"))
    detail = value.get("request")
    if not isinstance(detail, dict):
        raise ValueError("交互内容无效")
    if value.get("kind") == "question":
        questions = detail.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
            raise ValueError("问题数量超出限制")
        ids = []
        for q in questions:
            ids.append(string(q.get("id"), 128))
            string(q.get("question"), 4096)
            if q.get("header") is not None:
                string(q["header"], 256)
            if type(q.get("multiSelect", False)) is not bool:
                raise ValueError("问题选择方式无效")
            options = q.get("options", [])
            if not isinstance(options, list) or len(options) > 12:
                raise ValueError("问题选项过多")
            labels = []
            for option in options:
                labels.append(string(option.get("label"), 512))
                if option.get("description"):
                    string(option["description"], 2048)
            if len(set(labels)) != len(labels):
                raise ValueError("问题选项重复")
        if len(set(ids)) != len(ids):
            raise ValueError("问题编号重复")
    elif value.get("kind") == "approval":
        string(detail.get("tool_name"), 256)
        string(detail.get("reason"), 4096)
        if not isinstance(detail.get("arguments"), dict):
            raise ValueError("批准缺少完整工具参数")
    else:
        raise ValueError("交互类型无效")
    return value


def answer(item, value):
    if item["kind"] == "approval":
        if value not in ("allowed-once", "rejected"):
            raise ValueError("只能允许本次或拒绝")
        return value
    if not isinstance(value, dict) or set(value) != {"answers"} or not isinstance(value["answers"], list):
        raise ValueError("问题答复格式无效")
    questions = {q["id"]: q for q in item["request"]["questions"]}
    seen = set()
    for a in value["answers"]:
        if not isinstance(a, dict) or set(a) - {"id", "selected", "custom"}:
            raise ValueError("问题答复字段无效")
        qid = a.get("id")
        if not isinstance(qid, str) or qid not in questions or qid in seen:
            raise ValueError("问题编号不匹配")
        seen.add(qid)
        q = questions[qid]
        selected = a.get("selected")
        labels = {o["label"] for o in q.get("options", [])}
        if not isinstance(selected, list) or any(not isinstance(s, str) or s not in labels for s in selected):
            raise ValueError("请选择有效选项")
        if len(selected) != len(set(selected)) or (not q.get("multiSelect") and len(selected) > 1):
            raise ValueError("所选数量无效")
        custom = a.get("custom", "")
        if not isinstance(custom, str) or len(custom.encode()) > 2048 or not (selected or custom.strip()):
            raise ValueError("请回答每个问题，补充文字最多 2048 字节")
    if seen != set(questions):
        raise ValueError("请回答全部问题")
    return value
