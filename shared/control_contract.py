"""Small, explicitly versioned Host input contract; never transports RPC or credentials."""
import uuid


def command(value):
    if not isinstance(value, dict):
        raise ValueError("补充指令格式无效")
    action = value.get("action")
    fields = {"command_id", "epoch", "action"}
    if action in {"queue", "steer", "edit"}:
        fields.add("text")
        text = value.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16384:
            raise ValueError("请填写不超过 16 KiB 的补充内容")
    if action in {"edit", "remove", "promote"}:
        fields.update(("item_id", "revision"))
        uuid.UUID(value.get("item_id", ""))
        if type(value.get("revision")) is not int or value["revision"] < 0:
            raise ValueError("队列版本无效")
    elif action not in {"queue", "steer"}:
        raise ValueError("不支持此队列操作")
    if set(value) != fields:
        raise ValueError("补充指令字段无效")
    for key in ("command_id", "epoch"):
        uuid.UUID(value[key])
    return value


def state(value):
    uuid.UUID(value["epoch"])
    if type(value["revision"]) is not int or value["revision"] < 0 or not isinstance(value["items"], list) or len(value["items"]) > 20:
        raise ValueError("队列格式无效")
    ids = set()
    size = 0
    for item in value["items"]:
        uuid.UUID(item["item_id"]); uuid.UUID(item["request_id"])
        if item["item_id"] in ids or item["delivery"] not in {"queue", "steer"} or not isinstance(item["text"], str):
            raise ValueError("队列条目无效")
        ids.add(item["item_id"]); size += len(item["text"].encode())
    if size > 32768:
        raise ValueError("待执行补充内容合计不能超过 32 KiB")
    return value
