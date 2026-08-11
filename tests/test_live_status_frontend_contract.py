"""Static safety net for bounded, generation-aware live status polling.

Real-browser coverage owns rendering/timing.  These focused assertions make the
failure semantics reviewable even when a UI test server is unavailable.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
JS = (STATIC / "ancserTPX.js").read_text(encoding="utf-8")
HTML = (STATIC / "ancserTPX.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = JS.index(f"function {name}(")
    brace = JS.index("{", start)
    depth = 0
    for index in range(brace, len(JS)):
        if JS[index] == "{":
            depth += 1
        elif JS[index] == "}":
            depth -= 1
            if depth == 0:
                return JS[start:index + 1]
    raise AssertionError(f"unterminated JS function {name}")


def test_poll_runner_is_single_flight_bounded_and_generation_aware():
    runner = _function_source("_runBoundedLivePoll")
    assert "if (state.inFlight) return state.inFlight" in runner
    assert "const generation = ++state.generation" in runner
    assert "generation !== state.generation" in runner
    assert "generation === state.generation" in runner

    bounded = _function_source("_fetchJsonWithTimeout")
    assert "controller.abort()" in bounded
    assert "LIVE_STATUS_TIMEOUT_MS" in bounded
    assert "signal: controller.signal" in bounded


def test_main_status_preserves_running_but_marks_stale_or_degraded():
    stale = _function_source("_markLiveStatusStale")
    assert "_liveStatusPollState.lastGood" in stale
    assert "_liveStatusPollState.lastGoodAccountId === accountId" in stale
    assert "RUNNING · STATUS STALE" in stale

    poll = _function_source("pollLiveStatus")
    assert "_liveStatusPollState.lastGoodAccountId !== accountId" in poll
    assert "_liveStatusPollState.lastGood = null" in poll
    assert "_markLiveStatusStale(accountId)" in poll

    render = _function_source("_renderLiveStatus")
    assert "RUNNING · DEGRADED" in render
    assert "st.task_alive === false" in render
    assert "st.pi_listener_alive === false" in render
    assert "st.health === 'starting'" in render
    assert "if (!isStarting) _liveStartInProgress = false" in render


def test_slot_failure_reuses_last_good_map_instead_of_not_started():
    slots = _function_source("pollLiveSlots")
    assert "_liveSlotsPollState.lastGood || {}" in slots
    assert "_liveSlotRenderStatus(slot, statusMap, sess, true)" in slots
    slot_render = _function_source("_liveSlotRenderStatus")
    assert "RUNNING · STATUS STALE" in slot_render
    assert "RUNNING · DEGRADED" in slot_render


def test_visible_live_workspace_forces_fresh_poll_generations():
    assert "document.addEventListener('visibilitychange'" in JS
    assert "pollLiveStatus({ restart: true })" in JS
    assert "pollLiveSlots({ restart: true })" in JS


def test_stop_only_paints_stopped_after_confirmed_post_and_restarts_on_failure():
    stop = _function_source("stopLive")
    post_at = stop.index("fetch(API + '/live/stop'")
    clear_at = stop.index("clearInterval(_liveStatusInterval)")
    ok_at = stop.index("if (!resp.ok)")
    stopped_at = stop.index("statusEl.textContent = 'STOPPED'")
    assert clear_at < post_at < ok_at < stopped_at
    assert stop.count("_cancelLivePoll(_liveStatusPollState)") >= 3
    assert "_markLiveStatusStale(accountId)" in stop
    assert "_liveStatusInterval = setInterval(pollLiveStatus, 1000)" in stop
    assert "pollLiveStatus({ restart: true })" in stop


def test_backtest_loader_always_reselects_backend_workset_and_propagates_token():
    ensure = _function_source("_ensureBacktestData")
    build = _function_source("buildBacktestBody")
    retry = _function_source("_postBacktestWithWorksetRetry")
    run = _function_source("runBacktest")
    sweep = _function_source("runBacktestSweep")
    assert "body.workset_token = _btDataRange.worksetToken" in ensure
    assert "resp.status === 409" in ensure
    assert "body.append = false" in ensure
    assert "worksetToken: data.workset_token || ''" in ensure
    assert "Already loaded for this exact range" not in ensure
    assert "workset_token: (_btDataRange && _btDataRange.worksetToken) || ''" in build
    assert "resp.status !== 409" in retry
    assert "Object.assign(body, buildBacktestBody())" in retry
    assert "_postBacktestWithWorksetRetry" in run
    assert "_postBacktestWithWorksetRetry" in sweep


def test_active_script_url_busts_cache_for_status_health_code():
    assert 'ancserTPX.js?' in HTML
    assert '&statushealth=1"' in HTML
