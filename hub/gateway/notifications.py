"""Optional certificate-authenticated APNs sender; results remain useful without it."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import ssl
import time

from hub.gateway import inbox

LOGGER = logging.getLogger("hub.gateway.notifications")
# HTTPX INFO and httpcore DEBUG messages include URLs whose path contains the device token.
# Apply this even with an injected transport; a failed sender setup must not restore unsafe logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
MAX_ATTEMPTS = 5
MAX_AGE_SEC = 86400
STATUS_TEXT = {"done": "任务已完成", "failed": "任务执行失败", "cancelled": "任务已取消",
               "timed_out": "任务执行超时", "interrupted": "执行已中断，请查看结果"}


def validate_config(config: dict) -> dict:
    raw = config.get("apns", {})
    if not isinstance(raw, dict):
        raise ValueError("APNs 配置必须为对象")
    enabled = raw.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("APNs enabled 必须为布尔值")
    if not enabled:
        return {"enabled": False}
    environment = raw.get("environment")
    topic = raw.get("topic")
    certfile, keyfile = raw.get("certfile"), raw.get("keyfile")
    if environment not in {"sandbox", "production"}:
        raise ValueError("APNs 环境必须明确为 sandbox 或 production")
    if not isinstance(topic, str) or not 1 <= len(topic) <= 255 or re.fullmatch(r"[A-Za-z0-9.-]+", topic) is None:
        raise ValueError("APNs topic 必须为明确的 App bundle ID")
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in (certfile, keyfile)):
        raise ValueError("APNs 证书与私钥必须使用私有绝对路径")
    return {"enabled": True, "environment": environment, "topic": topic,
            "certfile": certfile, "keyfile": keyfile}


@dataclass(frozen=True)
class SendResult:
    status: int | None
    reason: str = ""


class APNsSender:
    def __init__(self, config: dict):
        # Import and read private PEMs only when explicitly configured. No JWT alternate path.
        import httpx
        # Keep request logging safe if another library changed its level after module import.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config["certfile"], config["keyfile"])
        self.environment = config["environment"]
        self.topic = config["topic"]
        host = "api.sandbox.push.apple.com" if self.environment == "sandbox" else "api.push.apple.com"
        self.client = httpx.AsyncClient(base_url=f"https://{host}", http1=False, http2=True,
                                       verify=context, timeout=10, trust_env=False,
                                       limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))

    async def send(self, token: str, payload: dict, apns_id: str, collapse_id: str) -> SendResult:
        expiration = int(time.time()) + MAX_AGE_SEC
        if expires := payload.get("xvis", {}).get("expires_at"):
            expiration = min(expiration, int(datetime.fromisoformat(expires).timestamp()))
        response = await self.client.post("/3/device/" + token, json=payload, headers={
            "apns-topic": self.topic, "apns-push-type": "alert", "apns-priority": "10",
            "apns-id": apns_id, "apns-collapse-id": collapse_id,
            "apns-expiration": str(expiration)})
        if response.http_version != "HTTP/2":
            return SendResult(None, "http2_required")
        reason = ""
        if response.status_code != 200:
            try:
                value = response.json().get("reason", "")
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
                    reason = value
            except (ValueError, AttributeError):
                pass
        return SendResult(response.status_code, reason)

    async def aclose(self) -> None:
        await self.client.aclose()


def create_sender(config: dict) -> APNsSender | None:
    if not config.get("enabled"):
        return None
    try:
        return APNsSender(config)
    except (ImportError, OSError, ValueError):
        # Credential paths, PEM contents and exception strings never enter logs.
        LOGGER.warning("APNs 发送器暂不可加载；普通任务和结果收件箱保持可用")
        return None


def payload_for(conn, row) -> dict:
    if row["result_id"].startswith("q-"):
        source = conn.execute("SELECT request_json FROM v1_interactions WHERE interaction_id=?", (row["result_id"],)).fetchone()
        return {"aps": {"alert": {"title": "XVIS", "body": "有操作需要你批准" if row["status"] == "approval" else "有问题需要回答"}, "sound": "default"},
                "xvis": {"server_id": inbox.server_id(conn), "task_id": row["task_id"], "interaction_id": row["result_id"],
                         "revision": row["revision"], "expires_at": json.loads(source[0])["expires_at"]}}
    return {"aps": {"alert": {"title": "XVIS", "body": STATUS_TEXT[row["status"]]}, "sound": "default"},
            "xvis": {"server_id": inbox.server_id(conn), "task_id": row["task_id"],
                     "result_id": row["result_id"], "revision": row["revision"]}}


def skip_ineligible(conn, *, interaction=False) -> None:
    table, source = outbox_tables(interaction)
    with conn:
        conn.execute(f"""UPDATE {table} SET state='skipped',last_reason='ineligible'
         WHERE state='pending' AND NOT EXISTS (
          SELECT 1 FROM {source} r JOIN v1_notification_devices d
           ON d.device_id={table}.device_id
          WHERE r.result_id={table}.result_id
           AND r.revision={table}.revision AND r.read_at IS NULL AND d.enabled=1)""")


def outbox_tables(interaction):
    return ("v1_interaction_notification_outbox", "v1_interaction_notification_source") if interaction else ("v1_notification_outbox", "v1_inbox_results")


async def send_pending(conn, sender, *, current_time: float | None = None, limit: int = 20, interaction=False) -> int:
    """Single Hub worker; commit attempt before await so restart retries retain their budget."""
    now = time.time() if current_time is None else current_time
    table, source = outbox_tables(interaction)
    skip_ineligible(conn, interaction=interaction)
    rows = list(conn.execute(f"""SELECT o.*,r.task_id,r.status,d.token,d.environment
     FROM {table} o JOIN {source} r ON r.result_id=o.result_id
     JOIN v1_notification_devices d ON d.device_id=o.device_id
     WHERE o.state='pending' AND o.next_attempt_at<=? ORDER BY o.id LIMIT ?""", (now, limit)))
    attempted = 0
    for row in rows:
        # A previous send can await while the user reads or disables the next recipient.
        fresh = conn.execute(f"""SELECT o.state,r.read_at,r.revision,d.enabled,d.token,d.environment,d.registration_nonce
         FROM {table} o JOIN {source} r ON r.result_id=o.result_id
         LEFT JOIN v1_notification_devices d ON d.device_id=o.device_id WHERE o.id=?""", (row["id"],)).fetchone()
        if fresh is None or fresh["state"] != "pending" or fresh["read_at"] is not None or fresh["revision"] != row["revision"] or not fresh["enabled"]:
            continue
        if fresh["environment"] != sender.environment:
            with conn:
                conn.execute(f"UPDATE {table} SET state='skipped',last_reason='environment' WHERE id=? AND state='pending'", (row["id"],))
            continue
        age_row = conn.execute(f"SELECT CAST(strftime('%s',created_at) AS INTEGER) FROM {table} WHERE id=?", (row["id"],)).fetchone()
        if row["attempts"] >= MAX_ATTEMPTS or (age_row[0] is not None and now - age_row[0] >= MAX_AGE_SEC):
            with conn:
                conn.execute(f"UPDATE {table} SET state='failed',last_reason='expired_or_exhausted' WHERE id=? AND state='pending'", (row["id"],))
            continue
        attempt = row["attempts"] + 1
        with conn:
            conn.execute(f"UPDATE {table} SET attempts=?,next_attempt_at=? WHERE id=? AND state='pending'",
                         (attempt, now + 30, row["id"]))
        try:
            result = await sender.send(fresh["token"], payload_for(conn, row), row["apns_id"],
                                       f'{row["result_id"]}-{row["revision"]}')
        except asyncio.CancelledError:
            raise
        except Exception:
            result = SendResult(None, "network_error")
        attempted += 1
        status, reason = result.status, result.reason
        retryable = status is None or status == 429 or (status is not None and 500 <= status <= 599)
        if status == 200:
            state, reason = "accepted", ""
        elif retryable and attempt < MAX_ATTEMPTS:
            state = "pending"
        else:
            state = "failed"
        # APNs 5XX responses require at least a fifteen minute wait; network/429 use bounded backoff.
        delay = max(900 if status is not None and 500 <= status <= 599 else 30,
                    min(900, 30 * 2 ** (attempt - 1)))
        safe_reason = reason if re.fullmatch(r"[A-Za-z0-9_]{0,64}", reason) else "invalid_response"
        with conn:
            conn.execute(f"UPDATE {table} SET state=?,next_attempt_at=?,last_reason=? WHERE id=? AND state='pending'",
                         (state, now + delay, safe_reason, row["id"]))
            if status == 410 or (status == 400 and reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}):
                # A re-registration may reuse the same token, including after DELETE.
                conn.execute("UPDATE v1_notification_devices SET enabled=0 WHERE device_id=? AND token=? AND registration_nonce=?",
                             (row["device_id"], fresh["token"], fresh["registration_nonce"]))
                skip_ineligible_without_transaction(conn)
    return attempted


def skip_ineligible_without_transaction(conn) -> None:
    for table in ("v1_notification_outbox", "v1_interaction_notification_outbox"):
        conn.execute(f"""UPDATE {table} SET state='skipped',last_reason='disabled'
         WHERE state='pending' AND NOT EXISTS (
          SELECT 1 FROM v1_notification_devices d
           WHERE d.device_id={table}.device_id AND d.enabled=1)""")


async def sender_loop(app) -> None:
    while True:
        try:
            await send_pending(app.state.db, app.state.apns_sender)
            await send_pending(app.state.db, app.state.apns_sender, interaction=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("通知发送暂未完成；结果已持久保存，将稍后重试")
        await asyncio.sleep(1)
