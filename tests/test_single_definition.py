"""Concepts that must be defined exactly once.

Why this exists: calculations previously drifted between production, research,
and frontend implementations without a failing test. Matching the definition
site keeps each registered concept in one executable source of truth.

The failure mode is not "someone wrote sloppy code". It is that an agent (or a
person) working inside one file has no reason to look in the other five, and
duplication is invisible until a number disagrees in production. So the check
has to live where it runs unattended.

Design notes, learned from a first attempt that was unusable:

* Match DEFINITIONS, not usages. Grepping the literal `0.25` hit 14 files —
  tick size, ratios, opacities. Grepping `TICK = 0.25` hits one.
* Curated, not automatic. A generic clone detector on a 46k-line repo produces
  noise nobody reads, and a check nobody reads is worse than none.
* Every entry names its home AND says what breaks if a second copy appears.
  A rule without a reason gets deleted the first time it is inconvenient.

To add a shared concept: define it once, register it here, done. To justify a
second copy: it has to be listed in `home` with a comment explaining why the
two cannot be the same function.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SOURCES = sorted(
    [p for p in ROOT.glob("backend/**/*.py") if "__pycache__" not in p.parts]
    + list(ROOT.glob("frontend/static/*.js"))
    # 1.5.1: Rust is scanned too. Before this, a Rust port of a registered
    # Python concept (e.g. exit.rs::resolve_pi_exit) was invisible here.
    + [p for p in ROOT.glob("research/rust_engine/**/*.rs") if "target" not in p.parts]
)


def _strip_comments(text: str, py: bool) -> str:
    """Blank out comments so a note explaining a removal is not a definition."""
    if py:
        return re.sub(r"(?m)^\s*#.*$", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


# concept -> (regex matching a DEFINITION, allowed files, what a second copy breaks)
SINGLE_DEFINITION = {
    "model exit-policy resolution": (
        r"def resolve_exit_policy\b|fn resolve_pi_exit\b",
        {"backend/strategy/exit_policy.py",
         # KNOWN UNMERGED: line-by-line Rust port of the PI branch. Merge is
         # stage 3 of docs/RUST_CORE_STANDARD.md (one definition via PyO3).
         "research/rust_engine/crates/live-runtime/src/exit.rs"},
        "A second resolver lets the same model select different time/trail "
        "semantics in live and backtest.",
    ),
    "active-position exit decision": (
        r"def evaluate_exit_operation\b",
        {"backend/strategy/exit_policy.py"},
        "Duplicated time/trail/ladder decisions let live and backtest drift "
        "even when they receive the same TradeSignal.",
    ),
    "price rounding to the exchange tick": (
        r"(?:def|fn)\s+_?(?:round_to_tick|round_tick|round_price|_round)\b",
        # KNOWN UNMERGED (2026-11-06): 11 copies in 10 files with at least
        # three behaviours (round to 4 vs 10 decimals; tick_size <= 0 returns
        # the price vs divides by zero; live/engine.py ignores its contract's
        # tick). Listing them stops a 12th; merging them is stage 1 of
        # docs/RUST_CORE_STANDARD.md and each removal shrinks this set.
        {"backend/backtest/prop_intraday_research.py",
         "backend/live/engine.py",
         "backend/strategy/exit_policy.py",
         "backend/strategy/factor.py",
         "backend/strategy/research_lab.py",
         "backend/strategy/sigma.py",
         "backend/strategy/volume_profile.py",
         "research/rust_engine/crates/live-runtime/src/pi.rs",
         "research/rust_engine/crates/vp-replay/src/profile.rs",
         "research/rust_engine/crates/vp-replay/src/strategy.rs"},
        "Two roundings of the same price can put a bracket one tick apart in "
        "live and backtest; ties at .5 tick already round differently.",
    ),
    "monthly normalisation (30.44 days)": (
        r"(DAYS_PER_MONTH|_ROB_DAYS_PER_MONTH)\s*=\s*30\.44|=\s*30\.44\b",
        {"backend/backtest/robustness.py"},
        "Two constants means two definitions of 'per month'; reports could "
        "disagree on the same run.",
    ),
    "walk-forward segmentation": (
        r"def segment_index|min\(2,\s*int\(off\s*\*\s*3",
        {"backend/backtest/robustness.py"},
        "The split decides which trades land in which third. Two copies means "
        "'walk-forward PASS' can mean different things in different reports.",
    ),
    "bar aggregation into higher timeframes": (
        r"def aggregate_candles\b|def bucket_start\b",
        {"backend/data/timeframes.py"},
        "Chart bars, the 5m factor markers and any research roll-up must bucket "
        "the same minutes (4h session anchor, Topstep trade date); a second "
        "copy draws different bars than the ones signals were computed on.",
    ),
    "strategy name normalisation": (
        r"def known_strategy_name\b|SUPPORTED_STRATEGIES\s*=\s*\(",
        {"backend/db/models.py"},
        "The 2026-09-15 preset rewrite: one loader did not know a strategy "
        "another build had written, and normalised it to factor on disk.",
    ),
    "contract economics table": (
        r"_CONTRACT_SPECS\s*=\s*\{",
        {"backend/db/models.py"},
        "A stale copy silently prices P&L wrong for whichever symbol drifted.",
    ),
    "strategy parameter fallback": (
        r"def strategy_param\b",
        {"backend/db/models.py"},
        "A second tr_* fallback rule can resolve different exits in live and backtest.",
    ),
    "monte carlo bootstrap": (
        r"def monte_carlo\b|function _robMonteCarlo\b",
        {"backend/backtest/robustness.py"},
        "The reason the maths moved out of the browser: a second bootstrap "
        "can drift outside pytest and backend reports.",
    ),
    "equity series stats (PF / maxDD walk)": (
        r"def series_stats\b|function _robSeriesStats\b",
        {"backend/backtest/robustness.py"},
        "This is the kernel under Monte Carlo, walk-forward AND the slip "
        "table; a second copy lets all three drift at once.",
    ),
    "PI option limit fill / stop-target-time exit": (
        r"def limit_price\b|def limit_fill_index\b|def exit_after\b",
        {"backend/options/pi_options.py"},
        "The PI OPTIONS backtest model, the published limit-entry / exit-grid "
        "studies (their scripts wrap these) and a future paper/live options "
        "path must fill and exit by the same rules, or the backtest stops "
        "describing what the account would have done.",
    ),
}

def _definition_sites(pattern: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in SOURCES:
        rel = path.relative_to(ROOT).as_posix()
        body = _strip_comments(
            path.read_text(encoding="utf-8", errors="replace"),
            path.suffix == ".py",
        )
        lines = [i for i, line in enumerate(body.splitlines(), 1)
                 if re.search(pattern, line)]
        if lines:
            found[rel] = lines
    return found


@pytest.mark.parametrize("concept", sorted(SINGLE_DEFINITION))
def test_concept_is_defined_in_exactly_one_place(concept):
    pattern, home, why = SINGLE_DEFINITION[concept]
    sites = _definition_sites(pattern)

    # Positive assertion first. A typo in the regex would otherwise make this
    # pass by finding nothing at all — the exact way a guard becomes decorative.
    assert sites, (
        f"{concept!r}: the pattern matched nothing. Either the definition was "
        f"renamed (update the pattern) or deleted (drop the entry). A check "
        f"that matches nothing protects nothing."
    )

    strays = {f: ls for f, ls in sites.items() if f not in home}
    assert not strays, (
        f"{concept!r} is defined outside its home.\n"
        f"  home  : {sorted(home)}\n"
        f"  strays: { {f: ls for f, ls in strays.items()} }\n"
        f"  why it matters: {why}\n"
        f"  Fix by calling the existing definition, or extract a shared helper "
        f"and change BOTH sides to call it. If a second copy is genuinely "
        f"unavoidable, add the file to `home` with a comment saying why."
    )


def test_registry_entries_all_point_at_files_that_exist():
    """A home that no longer exists makes its rule silently unenforceable."""
    for concept, (_, home, _why) in SINGLE_DEFINITION.items():
        for rel in home:
            assert (ROOT / rel).exists(), f"{concept!r} names a missing home: {rel}"


def test_the_detector_would_actually_catch_a_duplicate(tmp_path):
    """Guard the guard: prove a planted second definition is reported.

    Without this, a broken `_definition_sites` (wrong root, comment stripping
    that eats real code) would leave every rule above passing on an empty set.
    """
    pattern = r"def _probe_duplicate\b"
    assert not _definition_sites(pattern), "probe name is supposed to be unused"

    planted = ROOT / "backend" / "_probe_duplicate_tmp.py"
    planted.write_text("def _probe_duplicate():\n    return 1\n", encoding="utf-8")
    try:
        # SOURCES is captured at import; re-scan so the plant is visible.
        global SOURCES
        original = SOURCES
        SOURCES = original + [planted]
        assert _definition_sites(pattern) == {"backend/_probe_duplicate_tmp.py": [1]}
    finally:
        SOURCES = original
        planted.unlink()


def test_a_rust_duplicate_is_caught_and_rust_comments_do_not_count():
    """Guard the Rust half: a planted `fn` is reported, a `//` note is not."""
    global SOURCES
    pattern = r"fn _probe_duplicate\b"
    planted = ROOT / "research" / "rust_engine" / "_probe_duplicate_tmp.rs"
    planted.write_text(
        "// fn _probe_duplicate() was removed\n"
        "/* fn _probe_duplicate() */\n"
        "fn _probe_duplicate() -> i32 { 1 }\n",
        encoding="utf-8",
    )
    original = SOURCES
    try:
        SOURCES = original + [planted]
        assert _definition_sites(pattern) == {
            "research/rust_engine/_probe_duplicate_tmp.rs": [3]}
    finally:
        SOURCES = original
        planted.unlink()
    # Positive: the real tree is scanned, not just the plant.
    assert any(p.suffix == ".rs" for p in SOURCES)
    assert "research/rust_engine/crates/live-runtime/src/exit.rs" in _definition_sites(
        r"fn resolve_pi_exit\b")


def test_comments_naming_a_removed_definition_do_not_count():
    """This repo documents deletions in place; those notes must not trip a rule.

    `robustness.py` and `ancserTPX.js` both carry comments naming
    `_robMonteCarlo` precisely so nobody reintroduces it. Matching raw text
    would flag those comments and pressure the next person to delete the
    explanation — the same trap `_code()` exists for in the glass contracts.
    """
    js = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(
        encoding="utf-8", errors="replace")
    # Precondition: the notes naming the removed helpers are still in the file.
    assert "_robSeriesStats" in js
    assert "_robWalkForward" in js

    # Yet neither counts as a definition, because comments are stripped first.
    sites = _definition_sites(
        r"def series_stats\b|function _robSeriesStats\b"
        r"|def walk_forward\b|function _robWalkForward\b")
    assert "backend/backtest/robustness.py" in sites
    assert "frontend/static/ancserTPX.js" not in sites, (
        "a comment explaining a removal was counted as a definition; that "
        "pressures the next person to delete the explanation"
    )
