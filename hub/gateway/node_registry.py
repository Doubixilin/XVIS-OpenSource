"""单 worker Hub 的在线连接表；旧连接不得修改新连接。"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from fastapi import WebSocket
from hub.gateway.v1_repo import now_iso

@dataclass
class NodeState:
    node_id: str
    ws: WebSocket
    boot_id: str
    ready: bool = False
    idle_confirmed: bool = False
    running: list[str] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    last_seen: float = field(default_factory=time.monotonic)
    last_seen_iso: str = field(default_factory=now_iso)
    reconciliation: dict[str, dict | None] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.ws.send_json(message)

class NodeRegistry:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeState] = {}

    def register(self, node_id: str, ws: WebSocket, boot_id: str) -> tuple[NodeState, NodeState | None]:
        previous = self._nodes.get(node_id)
        state = NodeState(node_id=node_id, ws=ws, boot_id=boot_id)
        self._nodes[node_id] = state
        return state, previous

    def current(self, state: NodeState) -> bool:
        return self._nodes.get(state.node_id) is state

    def get(self, node_id: str) -> NodeState | None:
        return self._nodes.get(node_id)

    def unregister(self, state: NodeState) -> bool:
        if not self.current(state):
            return False
        del self._nodes[state.node_id]
        return True

    def touch(self, state: NodeState) -> None:
        if self.current(state):
            state.last_seen, state.last_seen_iso = time.monotonic(), now_iso()

    def expired(self, timeout: float) -> list[NodeState]:
        return [state for state in self._nodes.values() if time.monotonic() - state.last_seen > timeout]
