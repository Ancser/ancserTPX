"""1.0.9: Live 帳號槽設定—— 每帳號一 preset + 最多 2 個 live + 固定主帳號。

使用者實況(2026-07-05,依 Topstep Trading Accounts 畫面):
  - 帳號類型由名稱判定:含 EXPRESS → express(Express Funded,真錢);含 PRAC →
    practice;其餘(50KTC…Evaluation)→ exam(評估/考核,模擬)。
  - 不用 leader/follower —— 直接每帳號指定 preset;最多 **2 個帳號** 可 live + 記錄。
  - **main account** 是固定的那個 live 主帳號(trade-history 預設過濾 + shadow replay 用)。

設定持久化於 data/account_roles.json:
{
  "email": "<主 Topstep 登入 email>",
  "main_account_id": "<固定主帳號 id>",
  "accounts": { "<accId>": {"preset": "<preset 名>"|null, "live": true|false} }
}

規則:純設定/顯示層,不下單、不改任何交易行為。GO LIVE 一律由使用者手動觸發。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

ROLES_PATH = os.path.join("data", "account_roles.json")
MAX_LIVE = 2


def _empty_roles() -> Dict[str, Any]:
    return {"email": "", "main_account_id": "", "accounts": {}}


def classify_type(name: str) -> str:
    """帳號類型:express(真錢)/ practice(模擬)/ exam(評估,模擬)。依名稱判定。"""
    up = (name or "").upper()
    if "EXPRESS" in up:
        return "express"
    if "PRAC" in up:
        return "practice"
    return "exam"


def load_roles() -> Dict[str, Any]:
    """讀取持久化設定(缺檔/壞檔 → 空設定,絕不拋出)。"""
    try:
        if os.path.exists(ROLES_PATH):
            with open(ROLES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out = _empty_roles()
                out["email"] = str(data.get("email") or "")
                out["main_account_id"] = str(data.get("main_account_id") or "")
                accts = data.get("accounts") or {}
                if isinstance(accts, dict):
                    for k, v in accts.items():
                        v = v if isinstance(v, dict) else {}
                        preset = v.get("preset")
                        out["accounts"][str(k)] = {
                            "preset": (str(preset) if preset else None),
                            "live": bool(v.get("live")),
                        }
                return out
    except Exception:
        pass
    return _empty_roles()


def save_roles(roles: Dict[str, Any]) -> Dict[str, Any]:
    """驗證並寫入設定;回傳正規化版本。

    - 每帳號:preset(str|None)+ live(bool)。
    - 最多 MAX_LIVE 個 live —— 超出的(依輸入順序)一律降為 live=false。
    - main_account_id 必須是 accounts 內的鍵,否則清空;若可能,自動指向某個 live 帳號。
    """
    norm = _empty_roles()
    norm["email"] = str((roles or {}).get("email") or "")

    accts_in = (roles or {}).get("accounts") or {}
    live_count = 0
    for k, v in accts_in.items():
        v = v if isinstance(v, dict) else {}
        aid = str(k)
        preset = v.get("preset")
        live = bool(v.get("live"))
        if live:
            if live_count >= MAX_LIVE:
                live = False           # 超過 2 個 → 降級
            else:
                live_count += 1
        norm["accounts"][aid] = {
            "preset": (str(preset) if preset else None),
            "live": live,
        }

    main = str((roles or {}).get("main_account_id") or "")
    if main not in norm["accounts"]:
        main = ""
    # main 預設指向某個 live 帳號(若使用者沒指定或指定的非 live)
    live_ids = [aid for aid, c in norm["accounts"].items() if c["live"]]
    if (not main or not norm["accounts"].get(main, {}).get("live")) and live_ids:
        main = live_ids[0]
    norm["main_account_id"] = main

    os.makedirs(os.path.dirname(ROLES_PATH), exist_ok=True)
    with open(ROLES_PATH, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=2)
    return norm


def main_account_id(roles: Optional[Dict[str, Any]] = None) -> str:
    roles = roles or load_roles()
    return roles.get("main_account_id") or ""


def annotate_accounts(accounts: List[Dict[str, Any]],
                      roles: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把類型 + 持久化的 preset/live/main 套到 /accounts 清單上。

    每個帳號附加:
      account_type : "express" | "practice" | "exam"
      preset       : 指定的 preset 名(或 None)
      live         : 是否納入 2 個 live 之一
      is_main      : 是否為固定主帳號
    """
    roles = roles or load_roles()
    cfg = roles.get("accounts") or {}
    main = roles.get("main_account_id") or ""

    out: List[Dict[str, Any]] = []
    for acc in accounts:
        aid = str(acc.get("id"))
        c = cfg.get(aid) or {}
        item = dict(acc)
        atype = classify_type(acc.get("name", ""))
        item["account_type"] = atype
        item["type"] = atype.upper()                 # EXPRESS / PRACTICE / EXAM
        item["simulated"] = atype != "express"       # 只有 express 是真錢
        item["preset"] = c.get("preset")
        item["live"] = bool(c.get("live"))
        item["is_main"] = (aid == main)
        out.append(item)
    return out
