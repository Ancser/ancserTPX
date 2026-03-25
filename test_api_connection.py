# ============================================================
# 文件: test_api_connection.py
# 狀態: 已完成
# 問題: 無
# 關聯文件: -> backend/broker/topstepx.py, .env
# 函數結構:
#   - main() : 週一開市後跑這個，驗證 API 全鏈路
# ============================================================
"""
週一驗證腳本 — 跑一次確認 API 全通

用法: python test_api_connection.py
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# 直接用 httpx 測試，不依賴完整後端
import httpx

BASE_URL = "https://api.topstepx.com"
USERNAME = os.getenv("TOPSTEPX_USERNAME", "")
API_KEY = os.getenv("TOPSTEPX_API_KEY", "")


async def main():
    print("=" * 60)
    print("TopstepX API 連線測試")
    print("=" * 60)

    if not USERNAME or not API_KEY:
        print("[FAIL] .env 裡沒有 USERNAME 或 API_KEY")
        sys.exit(1)

    print(f"\n[1/5] 認證 (username: {USERNAME})")
    print(f"      POST {BASE_URL}/api/Auth/loginKey")

    async with httpx.AsyncClient(timeout=30) as client:
        # ── Step 1: 認證 ──
        try:
            resp = await client.post(
                f"{BASE_URL}/api/Auth/loginKey",
                json={"userName": USERNAME, "apiKey": API_KEY},
            )
            data = resp.json()
        except Exception as e:
            print(f"      [FAIL] 連線失敗: {e}")
            print("\n      如果是週末，這是正常的（期貨市場關閉）")
            print("      週日 5:00 PM CT (台灣週一 7:00 AM) 後重試")
            sys.exit(1)

        if not data.get("success"):
            print(f"      [FAIL] 認證失敗: {data}")
            print("\n      可能原因:")
            print("      - API Key 錯誤（重新從 TopstepX 複製）")
            print("      - Username 用 email 試試，或反過來")
            print("      - 週末維護中")
            sys.exit(1)

        token = data["token"]
        print(f"      [OK] Token 取得成功 (長度: {len(token)})")

        headers = {"Authorization": f"Bearer {token}"}

        # ── Step 2: 帳戶列表 ──
        print(f"\n[2/5] 取得帳戶列表")
        resp = await client.post(
            f"{BASE_URL}/api/Account/search",
            json={},
            headers=headers,
        )
        acc_data = resp.json()
        accounts = acc_data.get("accounts", [])
        print(f"      [OK] 找到 {len(accounts)} 個帳戶:")
        for acc in accounts:
            acc_type = "Practice" if "PRAC" in str(acc.get("name", "")) else "Funded"
            print(f"      - ID: {acc['id']}  名稱: {acc.get('name', '?')}  類型: {acc_type}")
            print(f"        餘額: ${acc.get('balance', 0):,.2f}  今日PnL: ${acc.get('openPnl', 0) + acc.get('closedPnl', 0):,.2f}")

        # 安全選擇：只用 Practice 帳戶
        practice_accounts = [a for a in accounts if "PRAC" in str(a.get("name", ""))]
        funded_accounts = [a for a in accounts if "PRAC" not in str(a.get("name", ""))]

        if funded_accounts:
            print(f"\n      [WARN] 偵測到 Funded 帳戶 — Bot 測試期間不會使用:")
            for fa in funded_accounts:
                print(f"         ID: {fa['id']}  名稱: {fa.get('name', '?')}  餘額: ${fa.get('balance', 0):,.2f}")

        if practice_accounts:
            account_id = practice_accounts[0]["id"]
            print(f"\n      [OK] 自動選擇 Practice 帳戶: ID={account_id}")
            print(f"      -> 建議填入 .env: TOPSTEPX_ACCOUNT_ID={account_id}")
        elif accounts:
            account_id = accounts[0]["id"]
            print(f"\n      [WARN] 沒找到 Practice 帳戶，預設用第一個: ID={account_id}")
            print(f"      -> 請確認這不是 Funded 帳戶！")
        else:
            account_id = None
            print(f"\n      [WARN] 沒有找到任何帳戶")

        # ── Step 3: 搜尋 NQ 合約 ──
        print(f"\n[3/5] 搜尋 NQ 合約")
        resp = await client.post(
            f"{BASE_URL}/api/Contract/search",
            json={"text": "NQ"},
            headers=headers,
        )
        con_data = resp.json()
        contracts = con_data.get("contracts", [])
        print(f"      [OK] 找到 {len(contracts)} 個 NQ 相關合約:")
        for c in contracts[:10]:  # 最多顯示 10 個
            print(f"      - ID: {c['id']}  名稱: {c.get('name', '?')}")

        # 找 E-mini NQ
        nq_full = [c for c in contracts if "ENQ" in c.get("id", "")]
        mnq = [c for c in contracts if "MNQ" in c.get("id", "") or "MENQ" in c.get("id", "")]

        if nq_full:
            print(f"\n      -> E-mini NQ (標準, $20/點): {nq_full[0]['id']}")
        if mnq:
            print(f"      -> Micro NQ (迷你, $2/點):   {mnq[0]['id']}")

        # ── Step 4: 取歷史 K 線 ──
        print(f"\n[4/5] 測試歷史 K 線 (5 分鐘)")
        test_contract = nq_full[0]["id"] if nq_full else (contracts[0]["id"] if contracts else None)

        if test_contract:
            resp = await client.post(
                f"{BASE_URL}/api/History/retrieveBars",
                json={
                    "contractId": test_contract,
                    "live": True,
                    "unit": 2,        # MINUTE
                    "unitNumber": 5,  # 5 分鐘
                    "limit": 10,
                },
                headers=headers,
            )
            bar_data = resp.json()
            bars = bar_data.get("bars", [])
            print(f"      [OK] 取得 {len(bars)} 根 5 分鐘 K 線")
            if bars:
                last = bars[-1]
                print(f"      最新: {last.get('t', '?')} O={last.get('o')} H={last.get('h')} L={last.get('l')} C={last.get('c')} V={last.get('v')}")

            # 測試 1 分鐘
            print(f"\n[5/5] 測試 1 分鐘 K 線 (回測精度)")
            resp = await client.post(
                f"{BASE_URL}/api/History/retrieveBars",
                json={
                    "contractId": test_contract,
                    "live": True,
                    "unit": 2,        # MINUTE
                    "unitNumber": 1,  # 1 分鐘
                    "limit": 10,
                },
                headers=headers,
            )
            bar_data = resp.json()
            bars_1m = bar_data.get("bars", [])
            print(f"      [OK] 取得 {len(bars_1m)} 根 1 分鐘 K 線")
            if bars_1m:
                last = bars_1m[-1]
                print(f"      最新: {last.get('t', '?')} O={last.get('o')} H={last.get('h')} L={last.get('l')} C={last.get('c')} V={last.get('v')}")
        else:
            print("      [SKIP] 沒找到合約，跳過")

    # ── 總結 ──
    print("\n" + "=" * 60)
    print("測試完成！")
    print("=" * 60)
    print(f"""
下一步:
  1. 把上面顯示的 Account ID 填入 .env
  2. 把正確的 Contract ID 填入 .env
  3. 跑 start.bat 啟動網頁
  4. 在網頁上執行回測
""")


if __name__ == "__main__":
    asyncio.run(main())
