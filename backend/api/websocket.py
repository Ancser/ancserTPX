# ============================================================
# 文件: backend/api/websocket.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   ← backend/main.py             (掛載 WS 端點)
#   → backend/broker/topstepx.py   (即時數據源)
#   → backend/strategy/consolidation.py (zone 事件)
# 函數結構:
#   - ConnectionManager.connect/disconnect/broadcast
#   - ws_endpoint(websocket) : WebSocket 主端點
# ============================================================
"""
WebSocket 即時推送

前端通過 ws://localhost:8000/ws 連接
接收 K 線、盤整區間、交易信號的即時更新
"""

from __future__ import annotations
import json
import logging
from typing import List

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 連接池管理"""

    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"WS 連接: {len(self.active)} 個客戶端")

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)
        logger.info(f"WS 斷開: {len(self.active)} 個客戶端")

    async def broadcast(self, message: dict):
        """廣播給所有連接的前端"""
        data = json.dumps(message, default=str)
        disconnected = []
        for ws in self.active:
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.active.remove(ws)


manager = ConnectionManager()


async def ws_endpoint(websocket: WebSocket):
    """WebSocket 主端點 /ws"""
    await manager.connect(websocket)
    try:
        while True:
            # 接收前端消息（如控制命令）
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "ping":
                await websocket.send_text(
                    json.dumps({"type": "pong"})
                )

    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── 推送輔助函數（供其他模組調用）────────────────────

async def broadcast_candle(candle_data: dict):
    await manager.broadcast({
        "type": "candle",
        "data": candle_data,
    })


async def broadcast_zone_update(zone_data: dict):
    await manager.broadcast({
        "type": "zone",
        "data": zone_data,
    })


async def broadcast_trade_signal(signal_data: dict):
    await manager.broadcast({
        "type": "signal",
        "data": signal_data,
    })


async def broadcast_position_update(position_data: dict):
    await manager.broadcast({
        "type": "position",
        "data": position_data,
    })
