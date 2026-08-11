const path = require("node:path");
const { expect, test } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);

async function openApp(page) {
  await page.addInitScript(() => {
    localStorage.setItem("ancserTPX.uiLang", "en");
    localStorage.setItem("ancserTPXTheme", "light");
  });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1") {
      await route.continue();
    } else if (url.hostname === "unpkg.com" && url.pathname.endsWith(
      "/lightweight-charts.standalone.production.js",
    )) {
      await route.fulfill({
        path: chartBundle,
        contentType: "application/javascript",
      });
    } else {
      await route.abort("blockedbyclient");
    }
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect.poll(() => page.evaluate(() => typeof _runBoundedLivePoll)).toBe("function");
}

test("live poll is single-flight and an aborted late response cannot win", async ({ page }) => {
  await openApp(page);

  const result = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    const pending = [];
    const seen = [];
    let calls = 0;
    window.fetch = (_url, options) => new Promise((resolve) => {
      calls += 1;
      pending.push({ resolve, signal: options.signal });
    });

    try {
      const state = { generation: 0, inFlight: null, controller: null, lastGood: null };
      const first = _runBoundedLivePoll(
        state, "/old", {},
        (payload) => seen.push(payload.value),
        () => seen.push("old-failed"),
      );
      const duplicate = _runBoundedLivePoll(
        state, "/duplicate", {},
        (payload) => seen.push(payload.value),
        () => seen.push("duplicate-failed"),
      );
      const samePromise = first === duplicate;

      const newer = _runBoundedLivePoll(
        state, "/new", { restart: true },
        (payload) => seen.push(payload.value),
        () => seen.push("new-failed"),
      );
      const oldWasAborted = pending[0].signal.aborted;

      pending[1].resolve({ ok: true, json: async () => ({ value: "new" }) });
      await newer;
      // Simulate a transport that ignores AbortSignal and resolves anyway.
      pending[0].resolve({ ok: true, json: async () => ({ value: "old" }) });
      await first;

      return { calls, samePromise, oldWasAborted, seen };
    } finally {
      window.fetch = originalFetch;
    }
  });

  expect(result).toEqual({
    calls: 2,
    samePromise: true,
    oldWasAborted: true,
    seen: ["new"],
  });
});

test("a failed refresh preserves last RUNNING and visibly marks it stale", async ({ page }) => {
  await openApp(page);

  const display = await page.evaluate(() => {
    _liveStatusPollState.lastGood = { running: true };
    _liveStatusPollState.lastGoodAccountId = "42";
    _markLiveStatusStale("42");
    const status = document.getElementById("live-status-text")
      || document.getElementById("lv-status-label");
    const dot = document.getElementById("live-status-dot");
    const matchingAccount = {
      text: status.textContent,
      color: status.style.color,
      title: status.title,
      dot: dot ? dot.style.background : null,
    };
    _markLiveStatusStale("84");
    return {
      matchingAccount,
      switchedAccountText: status.textContent,
    };
  });

  expect(display.matchingAccount.text).toBe("RUNNING · STATUS STALE");
  expect(display.matchingAccount.color).toBe("var(--amber)");
  expect(display.matchingAccount.title).toContain("last known state");
  expect(display.matchingAccount.dot).toBeNull();
  expect(display.switchedAccountText).toBe("STATUS STALE");
});

test("an alive-but-degraded PI engine is not painted as healthy RUNNING", async ({ page }) => {
  await openApp(page);

  const display = await page.evaluate(() => {
    const select = document.getElementById("live-acct-select-1");
    select.innerHTML = '<option value="42">PI TEST</option>';
    select.value = "42";
    _liveSlotRenderStatus(1, {
      "42": {
        running: true,
        health: "degraded",
        health_reasons: ["pi_listener_not_running"],
        strategy_mode: "pi",
        pi_listener_alive: false,
        phase: "PI idle",
        daily_pnl: 0,
        risk_gates: {},
      },
    }, { label: "RTH", color: "var(--green)" }, false);
    const degraded = {
      text: document.getElementById("live-slot-status-1").textContent,
      color: document.getElementById("live-slot-status-1").style.color,
      dot: document.getElementById("live-slot-dot-1").style.background,
      phase: document.getElementById("live-slot-phase-1").textContent,
    };
    _liveSlotRenderStatus(1, {
      "42": {
        running: true,
        health: "starting",
        starting: true,
        task_alive: false,
        strategy_mode: "pi",
        pi_listener_alive: false,
        phase: "PI idle",
        daily_pnl: 0,
        risk_gates: {},
      },
    }, { label: "RTH", color: "var(--green)" }, false);
    return {
      degraded,
      starting: {
        text: document.getElementById("live-slot-status-1").textContent,
        color: document.getElementById("live-slot-status-1").style.color,
      },
    };
  });

  expect(display).toEqual({
    degraded: {
      text: "RUNNING · DEGRADED",
      color: "var(--amber)",
      dot: "var(--amber)",
      phase: "PI idle",
    },
    starting: {
      text: "STARTING",
      color: "var(--amber)",
    },
  });
});

test("failed stop stays stale, keeps candle polling, and restarts status polling", async ({ page }) => {
  await openApp(page);

  const result = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    const oldCandleInterval = setInterval(() => {}, 60_000);
    _liveInterval = oldCandleInterval;
    _liveStatusInterval = setInterval(() => {}, 60_000);
    const account = _focusMainLiveAccount() || liveAccount;
    const accountId = account && account.id ? String(account.id) : "";
    _liveStatusPollState.lastGood = { running: true };
    _liveStatusPollState.lastGoodAccountId = accountId;
    window.fetch = async (url) => {
      if (String(url).includes("/live/stop")) {
        return { ok: false, status: 500, json: async () => ({ detail: "stop failed" }) };
      }
      return { ok: false, status: 503, json: async () => ({}) };
    };

    try {
      await stopLive();
      await new Promise((resolve) => setTimeout(resolve, 0));
      const status = document.getElementById("live-status-text")
        || document.getElementById("lv-status-label");
      return {
        text: status.textContent,
        candleLoopPreserved: _liveInterval === oldCandleInterval,
        statusLoopRestarted: _liveStatusInterval !== null,
      };
    } finally {
      if (_liveInterval) clearInterval(_liveInterval);
      if (_liveStatusInterval) clearInterval(_liveStatusInterval);
      _liveInterval = null;
      _liveStatusInterval = null;
      _cancelLivePoll(_liveStatusPollState);
      window.fetch = originalFetch;
    }
  });

  expect(result.candleLoopPreserved).toBe(true);
  expect(result.statusLoopRestarted).toBe(true);
  expect(result.text).toContain("STATUS STALE");
  expect(result.text).not.toBe("STOPPED");
});

test("late RUNNING response cannot repaint after confirmed stop", async ({ page }) => {
  await openApp(page);

  const result = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    let resolveStop;
    let resolveLateStatus;
    window.fetch = (url) => {
      if (String(url).includes("/live/stop")) {
        return new Promise((resolve) => { resolveStop = resolve; });
      }
      if (String(url).includes("/live/status")) {
        return new Promise((resolve) => { resolveLateStatus = resolve; });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };

    try {
      _liveStatusInterval = setInterval(() => {}, 60_000);
      const stopping = stopLive();
      await Promise.resolve();
      const latePoll = pollLiveStatus();
      await Promise.resolve();
      resolveStop({ ok: true, status: 200, json: async () => ({ success: true }) });
      await stopping;
      resolveLateStatus({
        ok: true,
        status: 200,
        json: async () => ({ running: true, health: "healthy", risk_gates: {} }),
      });
      await latePoll;
      const status = document.getElementById("live-status-text")
        || document.getElementById("lv-status-label");
      return {
        text: status.textContent,
        statusLoop: _liveStatusInterval,
        candleLoop: _liveInterval,
      };
    } finally {
      if (_liveInterval) clearInterval(_liveInterval);
      if (_liveStatusInterval) clearInterval(_liveStatusInterval);
      _liveInterval = null;
      _liveStatusInterval = null;
      _cancelLivePoll(_liveStatusPollState);
      window.fetch = originalFetch;
    }
  });

  expect(result.text).toBe("STOPPED");
  expect(result.statusLoop).toBeNull();
  expect(result.candleLoop).toBeNull();
});
