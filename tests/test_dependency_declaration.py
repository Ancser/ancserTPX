"""每個第三方 import 都必須在 requirements 裡宣告(DEPS-001 / DEPS-002)。

## 為什麼

第一次 CI 就掛了兩次,兩次都不是程式邏輯錯:

1. `requirements.txt` 裡有中文註解。**pip 用 locale 編碼讀 requirements 檔**,
   LANG 未設的 runner 解析成 ASCII → `UnicodeDecodeError`,一個測試都還沒跑
   就死了。
2. `matplotlib` 沒宣告。開發機全域裝了所以本機全綠,乾淨環境直接
   `ModuleNotFoundError`。

兩個都是「在我機器上可以」的經典型態,而且**只會在乾淨環境浮現** ——
也就是說,沒有這兩條測試的話,下一次還是得靠 CI 紅了才知道。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "requirements.txt"
REQ_DEV = ROOT / "requirements-dev.txt"

# 發行套件名 → import 名。只列名稱對不上的。
DIST_TO_IMPORT = {
    "python-dotenv": "dotenv",
    "pytest-subtests": "pytest_subtests",
    "signalrcore": "signalrcore",
    "tzdata": None,          # 只提供資料給 zoneinfo,沒有 import 名
}

# 本專案自己的頂層套件
LOCAL_PACKAGES = {"backend", "frontend", "scripts", "tests"}


def _declared_import_names() -> set[str]:
    names: set[str] = set()
    for f in (REQ, REQ_DEV):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            dist = line.split("==")[0].split(">=")[0].split("[")[0].strip().lower()
            mapped = DIST_TO_IMPORT.get(dist, dist)
            if mapped:
                names.add(mapped.replace("-", "_"))
    return names


def _third_party_imports(root: Path) -> dict[str, set[str]]:
    """回傳 {import 名: {用到它的檔案}}。含函式內的延遲 import。"""
    std = set(sys.stdlib_module_names)
    out: dict[str, set[str]] = {}
    for f in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m in std or m in LOCAL_PACKAGES or m.startswith("_"):
                    continue
                out.setdefault(m, set()).add(str(f.relative_to(ROOT)))
    return out


def test_scanner_actually_finds_imports():
    """正向斷言:掃描器壞掉回空 dict 時,下面的測試會全部變成假綠。"""
    found = _third_party_imports(ROOT / "backend")
    assert len(found) >= 5, f"只掃到 {sorted(found)} —— import 掃描壞了"
    assert "httpx" in found


def test_every_backend_third_party_import_is_declared():
    """DEPS-001:backend/ 用到的每個第三方套件都要在 requirements 裡。

    延遲 import(藏在函式裡的)也算 —— `matplotlib` 就是這樣漏掉的,
    它到執行到畫圖那一行才爆,而那時候已經在實盤了。
    """
    declared = _declared_import_names()
    used = _third_party_imports(ROOT / "backend")
    missing = {m: sorted(fs) for m, fs in used.items() if m not in declared}
    assert not missing, (
        "backend/ 有 import 但 requirements 沒宣告:\n" +
        "\n".join(f"  {m}  ← {', '.join(fs)}" for m, fs in sorted(missing.items()))
    )


@pytest.mark.parametrize("path", [REQ, REQ_DEV], ids=lambda p: p.name)
def test_requirements_files_are_ascii_only(path):
    """DEPS-002:pip 用 **locale** 編碼讀 requirements,不是 UTF-8。

    LANG 未設的 Linux runner 會解析成 ASCII,一個非 ASCII 字元就讓
    `pip install -r` 在跑任何測試之前先死。這正是 6e1b915 第一次 CI 失敗的原因。
    """
    raw = path.read_bytes()
    bad = [(i, hex(b)) for i, b in enumerate(raw) if b > 127]
    assert not bad, (
        f"{path.name} 含非 ASCII 位元組 {bad[:5]}。"
        f"requirements 檔的註解只能用 ASCII。"
    )


@pytest.mark.parametrize("path", [REQ, REQ_DEV], ids=lambda p: p.name)
def test_requirements_files_parse(path):
    """基本可解析性:每一行不是註解就得是合法的需求行。"""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        assert " " not in line, f"{path.name}:{n} 需求行含空白: {line!r}"


def test_runtime_requirements_are_pinned():
    """執行期依賴要鎖版本 —— 交易行為對 pandas/numpy 的變更敏感。

    tzdata 例外:它是純資料,而且我們要的就是最新的時區規則。
    """
    unpinned = []
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        if "==" not in line and line.lower() not in ("tzdata",):
            unpinned.append(line)
    assert not unpinned, f"執行期依賴未鎖版本: {unpinned}"
