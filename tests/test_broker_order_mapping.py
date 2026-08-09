"""TopstepX broker adapter 的送單語意測試。

## 為什麼這支檔案存在

`backend/broker/topstepx.py` 有 1,089 行、**先前一個測試都沒有**,而它是整個
系統裡唯一真的把單子送到券商的模組。同一天稍早的教訓是:`ast.parse` 通過、
import 通過、142 個測試全綠,仍然可以有一條路徑整條是壞的 —— 只要沒人測它。

這裡測的都是**不需要網路**的部分:列舉映射、payload 組裝、安全護欄。
成交/延遲那類要真的連線才測得到的,不在這裡(見治理文件 §21 Level B)。

## 最容易致命的一點

內部慣例與 ProjectX API 的列舉**不一樣**,而且是靜默的:

    內部 side  1=Buy  2=Sell        API side  0=Bid(buy)  1=Ask(sell)
    內部 type  3=Stop               API type  4=Stop      (API 沒有 3)

映射寫反不會拋例外 —— 它會**用正確的價格送出方向相反的單**。
"""
import inspect

import pytest

from backend.broker.topstepx import (
    TopstepXClient,
    _parse_contract_expiry,
    _previous_quarter,
    contract_roll_start,
    order_error_meaning,
)
from backend.db.models import OrderRequest


def _payload_for(order: OrderRequest) -> dict:
    """從 place_order 原始碼推不出 payload,所以用一個假的 http client 攔下來。

    比起把 payload 組裝抽成函式再測,這樣測的是**實際會送出去的東西**,
    包含任何後續加上的欄位。
    """
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "orderId": 1}

        text = '{"success": true, "orderId": 1}'

    class _Client:
        async def request(self, method, path, json=None, **kw):
            captured["method"] = method
            captured["path"] = path
            captured["json"] = json
            return _Resp()

    import asyncio

    c = TopstepXClient(username="u", api_key="k")
    c._ensure_http = lambda: _fake_coro(_Client())
    c._token = "tok"
    asyncio.run(c.place_order(order))
    return captured


async def _fake_coro(v):
    return v


def _order(**kw):
    base = dict(account_id=1, contract_id="CON.F.US.MNQ.U26", order_type=2, side=1, size=1)
    base.update(kw)
    return OrderRequest(**base)


class TestSideAndTypeMapping:
    """內部列舉 → API 列舉。寫反不會報錯,只會下反方向的單。"""

    def test_buy_maps_to_api_bid_zero(self):
        assert _payload_for(_order(side=1))["json"]["side"] == 0

    def test_sell_maps_to_api_ask_one(self):
        assert _payload_for(_order(side=2))["json"]["side"] == 1

    def test_buy_and_sell_do_not_collapse(self):
        """最重要的一條:兩個方向必須映射到不同的值。"""
        buy = _payload_for(_order(side=1))["json"]["side"]
        sell = _payload_for(_order(side=2))["json"]["side"]
        assert buy != sell

    def test_market_type_passes_through(self):
        assert _payload_for(_order(order_type=2))["json"]["type"] == 2

    def test_limit_type_passes_through(self):
        assert _payload_for(_order(order_type=1, limit_price=100.0))["json"]["type"] == 1

    def test_internal_stop_three_becomes_api_four(self):
        """API 沒有 type 3。送 3 過去會被當成別的東西或被拒。"""
        assert _payload_for(_order(order_type=3, stop_price=100.0))["json"]["type"] == 4

    def test_api_type_three_is_never_emitted(self):
        for t in (1, 2, 3):
            assert _payload_for(_order(order_type=t, limit_price=1.0,
                                       stop_price=1.0))["json"]["type"] != 3


class TestPayloadShape:
    def test_posts_to_order_place(self):
        c = _payload_for(_order())
        assert c["method"] == "POST" and c["path"] == "/api/Order/place"

    def test_required_fields_present(self):
        j = _payload_for(_order())["json"]
        for k in ("accountId", "contractId", "type", "side", "size"):
            assert k in j, f"payload 缺 {k}"

    def test_brackets_are_forwarded_when_the_engine_sets_them(self):
        """引擎**會**帶 bracket(`_entry_brackets_for_signal`),adapter 必須原樣轉送。

        2026-08-08 更正:這裡原本斷言「payload 不含 bracket」,但那只在
        OrderRequest 欄位是 None 時成立 —— 恆真,而且從來沒碰到引擎的
        實際路徑。真正的合約是「引擎給了就要送出去,而且不能變形」。
        """
        j = _payload_for(_order(
            stop_loss_bracket={"ticks": -40, "type": 4},
            take_profit_bracket={"ticks": 120, "type": 1},
        ))["json"]
        assert j["stopLossBracket"] == {"ticks": -40, "type": 4}
        assert j["takeProfitBracket"] == {"ticks": 120, "type": 1}

    def test_brackets_omitted_only_when_engine_leaves_them_none(self):
        """None 才省略 —— 送 null 過去會被 API 當成明確的「無保護」。"""
        j = _payload_for(_order())["json"]
        assert "stopLossBracket" not in j
        assert "takeProfitBracket" not in j

    def test_size_is_forwarded_verbatim(self):
        assert _payload_for(_order(size=3))["json"]["size"] == 3


class TestPracticeAccountGuard:
    """護欄:測試期間不得操作 Funded 帳戶。"""

    def _client_with_accounts(self, accounts):
        import asyncio

        c = TopstepXClient(username="u", api_key="k")

        async def _get():
            return accounts

        c.get_accounts = _get
        return c, asyncio

    def test_practice_account_allowed(self):
        c, aio = self._client_with_accounts([{"id": 7, "name": "PRACTICEJUL"}])
        assert aio.run(c.verify_practice_account(7)) is True

    def test_funded_account_blocked(self):
        c, aio = self._client_with_accounts([{"id": 9, "name": "XFA-12345"}])
        assert aio.run(c.verify_practice_account(9)) is False

    def test_unknown_account_blocked(self):
        """帳號查不到時必須**拒絕**,不是放行。"""
        c, aio = self._client_with_accounts([{"id": 1, "name": "PRAC"}])
        assert aio.run(c.verify_practice_account(999)) is False


class TestContractRollHelpers:
    """換月判定錯了會拿舊約的價格下新約的單。"""

    def test_parse_quarterly_codes(self):
        assert _parse_contract_expiry("CON.F.US.MNQ.U26") == (2026, 9)
        assert _parse_contract_expiry("CON.F.US.MES.Z25") == (2025, 12)
        assert _parse_contract_expiry("CON.F.US.MNQ.H26") == (2026, 3)
        assert _parse_contract_expiry("CON.F.US.MNQ.M26") == (2026, 6)

    def test_parse_rejects_garbage(self):
        assert _parse_contract_expiry("not-a-contract") is None

    def test_previous_quarter_wraps_year(self):
        assert _previous_quarter(2026, 3) == (2025, 12)
        assert _previous_quarter(2026, 6) == (2026, 3)

    def test_roll_start_precedes_expiry_month(self):
        d = contract_roll_start("CON.F.US.MNQ.U26")
        assert d is not None
        # 第三個週五往前 8 天 → 一定落在 9 月上旬
        assert (d.year, d.month) == (2026, 9) and d.day < 15


class TestErrorMeanings:
    def test_known_codes_are_described(self):
        assert order_error_meaning(1) and order_error_meaning(1) != str(1)

    def test_unknown_code_does_not_crash(self):
        assert isinstance(order_error_meaning(99999), str)
        assert isinstance(order_error_meaning(None), str)


def test_place_order_has_no_hardcoded_account():
    """防呆:送單路徑不得出現寫死的帳號或合約。"""
    src = inspect.getsource(TopstepXClient.place_order)
    assert "order.account_id" in src
    assert "order.contract_id" in src
