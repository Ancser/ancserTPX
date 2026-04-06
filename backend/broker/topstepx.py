# ============================================================
# 文件: backend/broker/topstepx.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   <- backend/api/websocket.py  (即時數據流)
#   <- backend/api/routes.py     (REST 數據/下單)
#   <- backend/risk/manager.py   (帳戶查詢/強制平倉)
#   -> backend/db/models.py      (Candle, OrderRequest, etc.)
# 函數結構:
#   - TopstepXClient.__init__(username, api_key, base_url)
#   - TopstepXClient.authenticate() -> str (JWT token)
#   - TopstepXClient.get_accounts() -> list
#   - TopstepXClient.search_contracts(text) -> list
#   - TopstepXClient.get_historical_bars(contract_id, unit, num, start, end) -> List[Candle]
#   - TopstepXClient.place_order(order) -> OrderResponse
#   - TopstepXClient.cancel_order(account_id, order_id) -> bool
#   - TopstepXClient.get_positions(account_id) -> list
#   - TopstepXClient.close_position(account_id, contract_id) -> OrderResponse
#   - TopstepXClient.connect_market_ws() -> SignalR connection
#   - TopstepXClient.subscribe_trades(contract_id, callback)
#   - TopstepXClient.subscribe_quotes(contract_id, callback)
#   - TopstepXClient.disconnect()
# ============================================================
"""
TopstepX / ProjectX Gateway API 封裝

API 文檔: https://gateway.docs.projectx.com/
REST Base: https://api.topstepx.com
SignalR Hub: https://rtc.topstepx.com/hubs/market (市場數據)
             https://rtc.topstepx.com/hubs/user   (帳戶事件)

認證: API Key + Username -> JWT Token (24h)
費用: $29/月 (折扣碼 "topstep" -> $14.50/月)

重要限制:
  - 成交量是平台量，非 CME 全量
  - 歷史數據最小粒度 = 秒 (unit=1), 無 tick 回溯
  - 即時 tick 通過 SignalR SubscribeContractTrades
  - 每次 retrieveBars 最多 20,000 根
"""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx

from backend.db.models import (
    Candle, OrderRequest, OrderResponse, AccountInfo, BarUnit
)

logger = logging.getLogger(__name__)


class TopstepXClient:
    """TopstepX API 客戶端（REST + SignalR）"""

    # TopstepX Production URLs
    DEFAULT_REST_URL = "https://api.topstepx.com"
    DEFAULT_MARKET_HUB = "https://rtc.topstepx.com/hubs/market"
    DEFAULT_USER_HUB = "https://rtc.topstepx.com/hubs/user"

    # Demo 環境（測試用）
    DEMO_REST_URL = "https://gateway-api-demo.s2f.projectx.com"

    def __init__(
        self,
        username: str,
        api_key: str,
        base_url: Optional[str] = None,
        use_demo: bool = False,
    ):
        self.username = username
        self.api_key = api_key
        self.base_url = base_url or (
            self.DEMO_REST_URL if use_demo else self.DEFAULT_REST_URL
        )
        self.token: Optional[str] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._signalr_market = None
        self._signalr_user = None
        self._callbacks: Dict[str, List[Callable]] = {}

    # ── 認證 ─────────────────────────────────────────

    async def authenticate(self) -> str:
        """
        API Key 認證，取得 JWT Token

        POST /api/Auth/loginKey
        {
            "userName": "...",
            "apiKey": "..."
        }

        Token 有效期約 24 小時
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/Auth/loginKey",
                json={
                    "userName": self.username,
                    "apiKey": self.api_key,
                },
                timeout=30,
            )
            data = resp.json()

        if not data.get("success"):
            raise AuthError(
                f"認證失敗: {data.get('errorMessage', 'unknown error')}"
            )

        self.token = data["token"]
        logger.info("TopstepX 認證成功")
        return self.token

    async def _ensure_http(self) -> httpx.AsyncClient:
        """確保 HTTP client 存在且有 token"""
        if not self.token:
            await self.authenticate()
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30,
            )
        return self._http

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """通用 REST 請求 (自動重試認證)"""
        client = await self._ensure_http()

        resp = await client.request(method, path, **kwargs)

        # Token 過期 -> 重新認證
        if resp.status_code == 401:
            logger.warning("Token 過期，重新認證...")
            await self.authenticate()
            client = await self._ensure_http()
            resp = await client.request(method, path, **kwargs)

        # Rate limit — exponential backoff
        if resp.status_code == 429:
            for wait in (3, 5, 10):
                logger.warning(f"Rate limited (429), 等待 {wait}s...")
                await asyncio.sleep(wait)
                resp = await client.request(method, path, **kwargs)
                if resp.status_code != 429:
                    break

        resp.raise_for_status()
        return resp.json()

    # ── 帳戶 ─────────────────────────────────────────

    async def get_accounts(self) -> List[Dict]:
        """
        取得帳戶列表
        POST /api/Account/search
        """
        data = await self._request("POST", "/api/Account/search", json={})
        return data.get("accounts", [])

    async def get_account_info(self, account_id: int) -> AccountInfo:
        """取得帳戶詳細資訊"""
        accounts = await self.get_accounts()
        for acc in accounts:
            if acc.get("id") == account_id:
                return AccountInfo(
                    account_id=acc["id"],
                    name=acc.get("name", ""),
                    balance=acc.get("balance", 0),
                    open_pnl=acc.get("openPnl", 0),
                    closed_pnl=acc.get("closedPnl", 0),
                    daily_pnl=acc.get("openPnl", 0) + acc.get("closedPnl", 0),
                )
        raise ValueError(f"帳戶 {account_id} 不存在")

    # ── 合約 ─────────────────────────────────────────

    async def search_contracts(self, text: str = "NQ") -> List[Dict]:
        """
        搜尋合約
        POST /api/Contract/search

        注意: API 需要 'searchText' (非 'text') 且 'live' 必須為 False
        """
        data = await self._request(
            "POST", "/api/Contract/search",
            json={"searchText": text, "live": False}
        )
        return data.get("contracts", [])

    async def get_nq_contract_id(self) -> str:
        """取得當前 NQ 合約 ID (e.g. CON.F.US.ENQ.H26)"""
        contracts = await self.search_contracts("NQ")
        # 找最近到期的 NQ 合約
        nq_contracts = [
            c for c in contracts
            if "ENQ" in c.get("id", "") or "NQ" in c.get("name", "")
        ]
        if not nq_contracts:
            raise ValueError("找不到 NQ 合約")
        # 按到期日排序，取最近的
        return nq_contracts[0]["id"]

    async def _resolve_contract_id(self, short_id: str) -> str:
        """
        將簡短合約 ID 解析為完整 CON.F 格式

        NQM26 -> CON.F.US.ENQ.M26
        MNQM26 -> CON.F.US.MNQ.M26

        如果已搜過則直接返回快取結果
        """
        # 常見映射
        mappings = {
            "NQ": "ENQ",   # E-mini NASDAQ
            "MNQ": "MNQ",  # Micro E-mini NASDAQ
            "ES": "ES",    # E-mini S&P 500
            "MES": "MES",  # Micro E-mini S&P 500
        }

        # 解析 shortId: NQM26 -> symbol=NQ, month=M, year=26
        symbol = ""
        month_year = ""
        for prefix in sorted(mappings.keys(), key=len, reverse=True):
            if short_id.upper().startswith(prefix):
                symbol = prefix
                month_year = short_id[len(prefix):]
                break

        if symbol and month_year and len(month_year) >= 2:
            full_id = f"CON.F.US.{mappings[symbol]}.{month_year}"
            logger.info(f"Contract ID resolved: {short_id} -> {full_id}")
            return full_id

        # Fallback: 搜索 API
        logger.info(f"Searching for contract: {short_id}")
        contracts = await self.search_contracts(short_id)
        if contracts:
            return contracts[0]["id"]

        raise ValueError(f"無法解析合約 ID: {short_id}")

    # ── 歷史數據 ──────────────────────────────────────

    async def get_historical_bars(
        self,
        contract_id: str,
        unit: BarUnit = BarUnit.MINUTE,
        unit_number: int = 5,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 20000,
    ) -> List[Candle]:
        """
        取得歷史 K 線數據

        POST /api/History/retrieveBars

        Args:
            contract_id: e.g. "CON.F.US.ENQ.H26"
            unit:        BarUnit.SECOND=1, MINUTE=2, HOUR=3, DAY=4
            unit_number: 1=1分鐘, 5=5分鐘, etc.
            start_time:  ISO 格式 "2026-03-01T00:00:00Z"
            end_time:    ISO 格式
            limit:       最多 20000 根

        Returns:
            List[Candle]

        回測用法:
            # 取 1 分鐘 K 線（精度足夠驗證 5 分鐘策略）
            bars = await client.get_historical_bars(
                contract_id, BarUnit.MINUTE, 1,
                "2026-03-01T00:00:00Z", "2026-03-21T00:00:00Z"
            )
        """
        # 如果 contract_id 不是完整格式，嘗試解析為 CON.F 格式
        if not contract_id.startswith("CON."):
            # NQM26 -> CON.F.US.ENQ.M26
            contract_id = await self._resolve_contract_id(contract_id)

        # 確保 startTime/endTime 有值 (API 要求必填)
        if not start_time or not end_time:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            if not end_time:
                end_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if not start_time:
                # 默認取最近 5 天
                start_dt = now - timedelta(days=5)
                start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        body: Dict[str, Any] = {
            "contractId": contract_id,
            "live": False,
            "unit": unit.value,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": False,
            "startTime": start_time,
            "endTime": end_time,
        }

        data = await self._request(
            "POST", "/api/History/retrieveBars",
            json=body
        )

        bars_raw = data.get("bars") or []
        if not data.get("success"):
            logger.warning(f"retrieveBars failed: errorCode={data.get('errorCode')}, "
                          f"errorMessage={data.get('errorMessage')}")
            return []
        interval_str = self._make_interval_str(unit, unit_number)

        candles = []
        for bar in bars_raw:
            candles.append(Candle(
                timestamp=datetime.fromisoformat(
                    bar["t"].replace("Z", "+00:00")
                ),
                open=float(bar["o"]),
                high=float(bar["h"]),
                low=float(bar["l"]),
                close=float(bar["c"]),
                volume=int(bar["v"]),
                symbol="NQ",
                interval=interval_str,
            ))

        logger.info(f"取得 {len(candles)} 根 {interval_str} K 線")
        return candles

    async def get_historical_bars_paginated(
        self,
        contract_id: str,
        unit: BarUnit = BarUnit.MINUTE,
        unit_number: int = 1,
        start_time: str = "",
        end_time: str = "",
    ) -> List[Candle]:
        """
        分頁取得大量歷史數據（超過 20000 根時自動分頁）

        適用場景：取 1 個月的 1 分鐘數據（約 20 x 390 = 7800 根/月）
        """
        all_candles: List[Candle] = []
        current_start = start_time

        while True:
            batch = await self.get_historical_bars(
                contract_id, unit, unit_number,
                current_start, end_time, limit=20000,
            )
            if not batch:
                break

            all_candles.extend(batch)

            # 如果拿到 < 20000 根，已經到底
            if len(batch) < 20000:
                break

            # 下一頁從最後一根的下一秒開始
            last_ts = batch[-1].timestamp
            current_start = last_ts.isoformat()

            # 避免 rate limit
            await asyncio.sleep(1)

        return all_candles

    # ── 帳戶安全 ─────────────────────────────────────

    _safe_account_ids: set = set()  # Practice 帳戶白名單

    async def verify_practice_account(self, account_id: int) -> bool:
        """
        驗證帳戶是否為 Practice 帳戶
        Bot 測試期間禁止操作 Funded 帳戶
        """
        if account_id in self._safe_account_ids:
            return True

        accounts = await self.get_accounts()
        for acc in accounts:
            if acc.get("id") == account_id:
                name = str(acc.get("name", ""))
                if "PRAC" in name:
                    self._safe_account_ids.add(account_id)
                    return True
                else:
                    logger.error(
                        f"[BLOCK] 帳戶 {account_id} ({name}) 不是 Practice 帳戶！"
                        f" Bot 測試期間禁止操作 Funded 帳戶。"
                    )
                    return False

        logger.error(f"帳戶 {account_id} 不存在")
        return False

    # ── 訂單 ─────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        下單

        POST /api/Order/place
        type: 1=Limit, 2=Market, 3=Stop
        side: 1=Buy, 2=Sell

        """
        # ProjectX API enums (integers):
        #   side:  0=Bid(buy), 1=Ask(sell)
        #   type:  1=Limit, 2=Market, 4=Stop, 5=TrailingStop (NO type 3!)
        # Internal convention: side 1=Buy, 2=Sell → convert to API 0/1
        api_side = 0 if order.side == 1 else 1  # 1(Buy)→0(Bid), 2(Sell)→1(Ask)
        # Internal convention: type 3=Stop → API type 4=Stop (API skips 3)
        api_type = 4 if order.order_type == 3 else order.order_type

        order_types = {1: "Limit", 2: "Market", 3: "Stop(internal)", 4: "Stop(API)"}
        order_sides = {1: "BUY", 2: "SELL"}
        logger.info(
            f"[ORDER SEND] {order_sides.get(order.side, '?')} {order_types.get(order.order_type, '?')} "
            f"size={order.size} limit={order.limit_price} stop={order.stop_price} "
            f"account={order.account_id} contract={order.contract_id} "
            f"(API: side={api_side}, type={api_type})"
        )

        payload = {
            "accountId": order.account_id,
            "contractId": order.contract_id,
            "type": api_type,
            "side": api_side,
            "size": order.size,
            "limitPrice": order.limit_price or 0,
            "stopPrice": order.stop_price or 0,
        }

        # Use direct request to capture 400 error body (not _request which raises)
        client = await self._ensure_http()
        raw_resp = await client.request("POST", "/api/Order/place", json=payload)

        # Token expired → retry
        if raw_resp.status_code == 401:
            await self.authenticate()
            client = await self._ensure_http()
            raw_resp = await client.request("POST", "/api/Order/place", json=payload)

        data = raw_resp.json()

        # Log full response on error for debugging
        if raw_resp.status_code >= 400:
            logger.error(
                f"[ORDER ERROR] HTTP {raw_resp.status_code} | payload={payload} | "
                f"response={data}"
            )

        resp = OrderResponse(
            order_id=data.get("orderId", 0),
            success=data.get("success", False),
            error_code=data.get("errorCode", 0),
            error_message=data.get("errorMessage"),
        )
        logger.info(
            f"[ORDER RESP] success={resp.success} order_id={resp.order_id} "
            f"error_code={resp.error_code} error_msg={resp.error_message} "
            f"raw_keys={list(data.keys())}"
        )
        return resp

    async def cancel_order(self, account_id: int, order_id: int) -> bool:
        """取消訂單 — requires both accountId and orderId per TopstepX API."""
        logger.info(f"[ORDER CANCEL] account={account_id} order_id={order_id}")
        try:
            payload = {"accountId": account_id, "orderId": order_id}
            client = await self._ensure_http()
            raw_resp = await client.request(
                "POST", "/api/Order/cancel", json=payload
            )

            # Token expired → retry
            if raw_resp.status_code == 401:
                await self.authenticate()
                client = await self._ensure_http()
                raw_resp = await client.request(
                    "POST", "/api/Order/cancel", json=payload
                )

            data = raw_resp.json()
            success = data.get("success", False)

            if raw_resp.status_code >= 400:
                logger.error(
                    f"[ORDER CANCEL ERROR] order_id={order_id} HTTP {raw_resp.status_code} | "
                    f"payload={payload} | response={data}"
                )
                return False

            logger.info(
                f"[ORDER CANCEL RESP] order_id={order_id} success={success} "
                f"HTTP={raw_resp.status_code}"
            )
            return success
        except Exception as e:
            logger.error(f"[ORDER CANCEL] order_id={order_id} exception: {e}")
            return False

    async def get_orders(self, account_id: int) -> List[Dict]:
        """查詢所有訂單 (open + filled + cancelled)"""
        data = await self._request(
            "POST", "/api/Order/search",
            json={"accountId": account_id}
        )
        logger.info(
            f"[ORDER SEARCH] account={account_id} | response_type={type(data).__name__} "
            f"keys={list(data.keys()) if isinstance(data, dict) else 'list'} "
            f"raw_preview={str(data)[:500]}"
        )
        orders = data.get("orders", data if isinstance(data, list) else [])
        logger.info(f"[ORDER SEARCH] parsed {len(orders)} orders")
        return orders

    async def get_open_orders(self, account_id: int) -> List[Dict]:
        """查詢未成交掛單 (使用 searchOpen 端點)"""
        data = await self._request(
            "POST", "/api/Order/searchOpen",
            json={"accountId": account_id}
        )
        orders = data.get("orders", data if isinstance(data, list) else [])
        return orders

    # ── 持倉 ─────────────────────────────────────────

    async def get_positions(self, account_id: int) -> List[Dict]:
        """查詢持倉 (使用 searchOpen 端點)"""
        data = await self._request(
            "POST", "/api/Position/searchOpen",
            json={"accountId": account_id}
        )
        positions = data.get("positions", data if isinstance(data, list) else [])
        if positions:
            logger.info(
                f"[POSITION] account={account_id} | count={len(positions)} | "
                f"details={[{k: p.get(k) for k in ('side','averagePrice','size','contractId','type')} for p in positions[:3]]}"
            )
        return positions

    async def close_position(
        self, account_id: int, contract_id: str
    ) -> OrderResponse:
        """平倉指定合約"""
        data = await self._request(
            "POST", "/api/Position/closeContract",
            json={
                "accountId": account_id,
                "contractId": contract_id,
            }
        )
        return OrderResponse(
            order_id=data.get("orderId", 0),
            success=data.get("success", False),
            error_code=data.get("errorCode", 0),
            error_message=data.get("errorMessage"),
        )

    async def flatten_all(self, account_id: int) -> List[OrderResponse]:
        """平倉所有持倉（3:05 PM CT 緊急平倉用）"""
        positions = await self.get_positions(account_id)
        results = []
        for pos in positions:
            if pos.get("size", 0) != 0:
                resp = await self.close_position(
                    account_id, pos["contractId"]
                )
                results.append(resp)
        return results

    # ── SignalR WebSocket（即時數據）────────────────────

    async def connect_market_ws(self):
        """
        連接 SignalR 市場數據 Hub

        需要 signalrcore 庫:
            pip install signalrcore

        Transport: WebSocket only, skipNegotiation=True
        """
        try:
            from signalrcore.hub_connection_builder import HubConnectionBuilder
        except ImportError:
            raise ImportError("請安裝 signalrcore: pip install signalrcore")

        hub_url = f"{self.DEFAULT_MARKET_HUB}?access_token={self.token}"

        self._signalr_market = (
            HubConnectionBuilder()
            .with_url(hub_url, options={
                "skip_negotiation": True,
                "headers": {"Authorization": f"Bearer {self.token}"},
            })
            .with_automatic_reconnect({
                "type": "interval",
                "intervals": [1, 3, 5, 10, 30],
            })
            .build()
        )

        # 註冊事件
        self._signalr_market.on("GatewayTrade", self._on_trade)
        self._signalr_market.on("GatewayQuote", self._on_quote)
        self._signalr_market.on("GatewayDepth", self._on_depth)

        self._signalr_market.start()
        logger.info("SignalR 市場數據連接成功")

    def subscribe_trades(
        self, contract_id: str, callback: Callable
    ):
        """
        訂閱逐筆成交（tick data）

        這是獲取即時 tick 的唯一方式（REST 無法回溯 tick）

        callback(contract_id, trade_data) 格式:
            trade_data = {"price": 21500.0, "size": 1, "timestamp": "..."}
        """
        if "trade" not in self._callbacks:
            self._callbacks["trade"] = []
        self._callbacks["trade"].append(callback)

        if self._signalr_market:
            self._signalr_market.send(
                "SubscribeContractTrades", [contract_id]
            )
            logger.info(f"訂閱 tick: {contract_id}")

    def subscribe_quotes(
        self, contract_id: str, callback: Callable
    ):
        """訂閱 Level 1 報價 (best bid/ask)"""
        if "quote" not in self._callbacks:
            self._callbacks["quote"] = []
        self._callbacks["quote"].append(callback)

        if self._signalr_market:
            self._signalr_market.send(
                "SubscribeContractQuotes", [contract_id]
            )

    def _on_trade(self, args):
        """SignalR GatewayTrade 回調"""
        for cb in self._callbacks.get("trade", []):
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"Trade callback error: {e}")

    def _on_quote(self, args):
        """SignalR GatewayQuote 回調"""
        for cb in self._callbacks.get("quote", []):
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"Quote callback error: {e}")

    def _on_depth(self, args):
        """SignalR GatewayDepth 回調"""
        for cb in self._callbacks.get("depth", []):
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"Depth callback error: {e}")

    # ── 工具方法 ──────────────────────────────────────

    @staticmethod
    def _make_interval_str(unit: BarUnit, unit_number: int) -> str:
        suffixes = {
            BarUnit.SECOND: "s",
            BarUnit.MINUTE: "m",
            BarUnit.HOUR: "h",
            BarUnit.DAY: "d",
            BarUnit.WEEK: "w",
            BarUnit.MONTH: "M",
        }
        return f"{unit_number}{suffixes.get(unit, '?')}"

    async def disconnect(self):
        """斷開所有連接"""
        if self._signalr_market:
            self._signalr_market.stop()
            self._signalr_market = None
        if self._signalr_user:
            self._signalr_user.stop()
            self._signalr_user = None
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("TopstepX 連接已斷開")


class AuthError(Exception):
    """認證失敗"""
    pass
