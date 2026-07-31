/* ============================================================
   ancserTPX — production liquid glass skin

   Transforms the real ancserTPX UI in place while preserving the
   production controls, handlers, data flow, and chart instances.

   ── THE RULE THAT SHAPES THIS WHOLE FILE ────────────────────────

   MOVE elements, never clone or rebuild them.

   ancserTPX.js binds behaviour by direct property assignment:

       document.querySelectorAll('.tab').forEach(t => { t.onclick = ... })

   and the markup carries inline handlers (onclick="toggleTrailTrigger('bt')",
   oninput="onEmapmoThresholdChange('bt')"). cloneNode does not copy
   property-assigned handlers, and replacing a node drops the binding
   silently — the tab would still highlight but the panel behind it
   would never switch. So every transform here re-parents and
   re-decorates the original node.

   Where a glass control cannot BE the original (a slider replacing an
   <input type=range>), the original stays in the DOM, hidden, and the
   glass control drives it through data-slider-proxy /
   data-switch-proxy, which tpx-glass.js forwards. That keeps the
   app's own handlers on the real event path.

   ── LOAD ORDER ──────────────────────────────────────────────────

   Must run before tpx-glass.js mounts its surfaces, because it decides
   which elements ARE surfaces. Both listen on DOMContentLoaded, so
   this script tag simply comes first.
   ============================================================ */
(() => {
    "use strict";

    const byId = (id) => document.getElementById(id);

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
        return node;
    }

    function opticalSpan(className, component, container = false) {
        const node = el("span", `optical-surface ${className}`);
        node.dataset.optical = component;
        if (container) node.setAttribute("data-container-glass", "");
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    /* Wrap a node's existing children in .control-source-content, which
       is what the lens masks and re-renders refracted. */
    function wrapContent(node, html) {
        const content = el("span", "control-source-content");
        if (html === undefined) {
            while (node.firstChild) content.appendChild(node.firstChild);
        } else {
            content.innerHTML = html;
        }
        node.appendChild(content);
        return content;
    }

    const DOCK_ICONS = {
        calendar: "⌁",
        backtest: "▦",
        live: "◉",
        account: "⚙",
    };

    /* ── ideal #1: header tabs -> drag dock floating on the chart ──── */
    function skinTopBar() {
        const header = document.querySelector(".header");
        const tabs = document.querySelector(".header-tabs");
        const chart = byId("chart-container");
        const main = document.querySelector(".main");
        if (!header || !tabs || !chart) return;

        // IDs are stripped from optical clones. Stable classes keep the
        // light-mode legend/menu palette identical inside the lens.
        byId("signal-legend")?.classList.add("glass-signal-legend");
        byId("lv-levels-panel")?.classList.add("glass-levels-panel");

        const topbar = el("div", "glass-topbar");

        // Brand, minus the version chip's inline styling.
        const h1 = header.querySelector("h1");
        const brand = el("div", "topbar-brand");
        brand.innerHTML = '<b>ancser</b>TPX <span class="ver">1.0.9</span>';
        if (h1) h1.remove();
        topbar.appendChild(brand);

        // The dock IS the original .header-tabs node, re-parented.
        tabs.classList.add("glass-dock");
        tabs.setAttribute("aria-label", "Workspace");
        [...tabs.querySelectorAll(".tab")].forEach((tab, index) => {
            tab.dataset.dockIndex = index;
            const label = tab.textContent.trim();
            const icon = DOCK_ICONS[tab.dataset.tab] || "•";
            tab.textContent = "";
            wrapContent(tab, `${icon}<small>${label}</small>`);
        });
        tabs.prepend(opticalSpan("dock-bubble", "dock"));
        tabs.prepend(opticalSpan("control-container-glass", "dockContainer", true));
        topbar.appendChild(tabs);

        // Account orb. The real credential form is re-parented into its
        // panel; automatic data controls remain live but visually hidden.
        const account = el("div", "glass-account");
        /* Real glass, same material as the chart menu button: mounting it as
           an optical surface under the `fab` tuning gives it an actual lens
           instead of only borrowing the glass border/relief tokens. Its stage
           resolves through the top bar's data-optical-stage, so it refracts
           the chart exactly like the FAB does. */
        const orb = el("button", "account-orb optical-surface", {
            type: "button", "aria-haspopup": "true", "aria-expanded": "false",
        });
        orb.dataset.optical = "fab";
        /* The engine prepends the lens layer, so the letter needs its own
           stacking context above it — and syncAccount must write here rather
           than to orb.textContent, which would wipe the layer out. */
        const orbLabel = el("span", "surface-content");
        orb.appendChild(orbLabel);
        const panel = el("div", "account-panel");
        const connWrap = document.querySelector(".conn-dropdown-wrap");
        const badge = byId("account-badge");
        const trigger = byId("conn-trigger");
        const username = byId("username");
        const apikey = byId("apikey");

        const hideRuntime = (node) => node?.classList.add("glass-runtime-anchor");
        hideRuntime(byId("contract-preset")?.closest(".form-group"));
        hideRuntime(byId("contract-id")?.closest(".form-group"));
        hideRuntime(byId("data-count")?.closest(".form-row"));
        hideRuntime(byId("data-range-info"));
        hideRuntime(byId("btn-fetch-full"));

        // The trigger's own dropdown behaviour is redundant once the orb
        // owns the disclosure; keep the node (ancserTPX.js writes its
        // status text) but neutralise the click and let it read as a row.
        if (trigger) {
            trigger.onclick = null;
            trigger.classList.add("account-conn-row");
        }
        if (connWrap) panel.appendChild(connWrap);

        const who = el("div", "who");
        panel.prepend(who);

        const syncAccount = () => {
            const email = username?.value.trim() || "";
            const state = document.documentElement.dataset.connectionState || "error";
            who.textContent = email || "EMAIL REQUIRED";
            orbLabel.textContent = email ? email[0].toUpperCase() : "?";
            orb.dataset.conn = state;
            orb.title = `${email || "Email required"} · ${state.toUpperCase()}`;
            orb.setAttribute("aria-label", orb.title);
        };
        syncAccount();
        new MutationObserver(syncAccount).observe(document.documentElement, {
            attributes: true, attributeFilter: ["data-connection-state"],
        });
        username?.addEventListener("input", syncAccount);
        apikey?.addEventListener("input", syncAccount);

        if (badge) {
            badge.classList.add("topbar-account-badge");
            const syncBadge = () => {
                const value = badge.textContent.trim();
                badge.hidden = !value || value === "--";
            };
            syncBadge();
            new MutationObserver(syncBadge).observe(badge, {
                attributes: true, attributeFilter: ["class"],
                childList: true, characterData: true, subtree: true,
            });
            account.appendChild(badge);
        }
        account.appendChild(orb);
        account.appendChild(panel);

        // Right cluster: theme switch sits immediately left of the orb.
        const right = el("div", "topbar-right");
        const themeTrack = el("button", "glass-switch topbar-theme", {
            type: "button", role: "switch", title: "Light / dark",
        });
        themeTrack.id = "theme-switch";
        themeTrack.dataset.stage = "switch";
        themeTrack.appendChild(opticalSpan("switch-thumb", "switch"));
        right.appendChild(themeTrack);
        right.appendChild(account);
        topbar.appendChild(right);

        // Keep the real clock node alive (the app writes to it every
        // second without a null guard), but remove it from the visual UI.
        const clock = byId("clock");
        if (clock) {
            clock.classList.add("glass-runtime-anchor");
            document.body.appendChild(clock);
        }
        document.querySelector("#lang-toggle")?.remove();
        header.remove();

        // One stationary workspace fog spans both the scrolling sidebar and
        // the chart. It remains pointer-transparent and follows .main's
        // visibility, so Research keeps its independent surface.
        /* Inside #chart-container, not .main. The dock's stage IS the chart,
           so a fog parented to .main is simply absent from the clone the dock
           refracts — the bar sampled the raw chart and ignored the fade. Over
           the sidebar the fog only ever covered flat padding, so nothing is
           lost by scoping it to the chart. */
        chart.prepend(el("div", "chart-fog"));
        /* The chart fog used to span .main, so it also faded the sidebar's
           content as it scrolled under the floating bar. Scoping it to the
           chart (above) took that away, so the sidebar gets its own — kept
           separate rather than one wide element because a fog covering both
           would have to live outside #chart-container, and then the dock
           could not sample it. */
        const sidebar = document.querySelector(".sidebar");
        if (sidebar && !sidebar.querySelector(":scope > .sidebar-fog")) {
            sidebar.prepend(el("div", "chart-fog sidebar-fog", { "aria-hidden": "true" }));
        }
        const watermark = el("div", "chart-watermark");
        watermark.innerHTML = "<b>ancser</b>TPX <small>1.0.9</small>";
        chart.prepend(watermark);
        chart.dataset.stage = "chart";

        /* The top bar lives on <body>, NOT inside #chart-container.
           The Research/Account tabs are full-page overlays that do
           mainEl.style.display='none', which would take the nav down
           with them and leave no way back. It still refracts the chart
           via data-optical-stage. */
        /* Candidate list, first visible wins (resolveStageSelector).
           Research hides .main, which zeroes #chart-container and would
           otherwise kill the lens on both the dock's container and its
           pill; the Research view is the fallback so every workspace has
           something real to refract. */
        topbar.dataset.opticalStage = '#chart-container, [data-stage="research"]';
        document.body.appendChild(topbar);

        // The sweep pair is an inline-styled exception to TPX's normal
        // .form-row structure. Mark it so the single-column skin can stack
        // the button and its model selector like every other sidebar option.
        byId("btn-sweep")?.parentElement?.classList.add("glass-single-stack");

        /* Spans the full viewport so the dock's centre is the SCREEN's
           centre. Offsetting the bar to start at the sidebar's right
           edge centred the dock on the chart instead, which is why the
           Research tab (no sidebar) looked centred and the other two
           looked pushed right. The sidebar gets padding-top in CSS so
           the floating bar never covers its first panel. */

        /* Magnifier tracking the cursor. Parented to .main, not to
           #chart-container, so it can travel over the sidebar as well
           as the chart — and its stage is .main for the same reason:
           the surface refracts whichever of the two it happens to be
           over. */
        const lensHost = main || chart;
        if (main) main.dataset.stage = "app";
        const appendLens = (stage) => {
            if (!stage) return;
            const lens = el("div", "optical-surface chart-lens");
            lens.dataset.optical = "precision";
            lens.setAttribute("aria-hidden", "true");
            stage.appendChild(lens);
        };
        appendLens(lensHost);

        // Research hides .main entirely, so it needs its own stage/lens.
        const research = byId("calendar-view");
        if (research) {
            research.dataset.stage = "research";
            if (!research.querySelector(":scope > .research-fog")) {
                /* data-optical-pin marks this as viewport-anchored: it is
                   position:fixed here, which a clone cannot reproduce, so
                   the engine re-pins it into clone space on every sync. */
                research.prepend(el("div", "chart-fog research-fog", {
                    "aria-hidden": "true",
                    "data-optical-pin": "viewport",
                }));
            }
            appendLens(research);
        }

        // Ideal #1: status line drops clear of the floating dock.
        const topBarStatus = byId("live-top-bar");
        if (topBarStatus) topBarStatus.classList.add("chart-status-shifted");
    }

    /* ── ideal #2: bottom tabs -> segment control ──────────────────── */
    function skinBottomTabs() {
        const tabs = document.querySelector(".bottom-tabs");
        const panel = byId("bottom-panel");
        if (!tabs || !panel) return;
        tabs.classList.add("glass-segment");
        tabs.setAttribute("aria-label", "Bottom panel");

        [...tabs.querySelectorAll(".bottom-tab")].forEach((tab, index) => {
            tab.dataset.segmentIndex = index;
            wrapContent(tab);
        });
        const indicator = opticalSpan("segment-indicator", "segment");
        /* The pill samples the whole workspace rather than the panel it
           sits in, so its rim carries the sidebar to the left, the chart
           above and the table below instead of running out of stage.
           Deliberately on the pill and NOT on the track: the container is
           1934px wide and its own reach is ~241px, so staging it here too
           would clone the workspace a second time for the widest surface
           on screen. Its edges are covered by the stage-matched surround
           fill instead. Sits inside the panel's data-stage, so the
           closest-marker rule in tpx-glass.js makes this win. */
        indicator.dataset.opticalStage = ".main";
        tabs.prepend(indicator);
        tabs.prepend(opticalSpan("control-container-glass", "segmentContainer", true));
        panel.dataset.stage = "bottom";
    }

    /* ── ideal #3: toggle-field -> switch, form-group -> param row ──── */
    function skinToggles() {
        document.querySelectorAll(".toggle-field").forEach((original) => {
            if (!original.id) original.id = `tf-${Math.random().toString(36).slice(2, 8)}`;
            const group = original.closest(".form-group") || original.parentElement;
            const label = group?.querySelector("label");

            const track = el("button", "glass-switch", {
                type: "button", role: "switch",
            });
            track.dataset.stage = "switch";
            track.dataset.switchProxy = original.id;
            if (original.classList.contains("on")) track.classList.add("on");
            track.appendChild(opticalSpan("switch-thumb", "switch"));

            // Keep the original in the DOM and on the event path; it is
            // what ancserTPX.js reads and writes.
            original.classList.add("glass-proxy-hidden");

            if (group && label) {
                group.classList.add("param-row");
                label.classList.add("param-label");
                const control = el("span", "param-control");
                control.appendChild(track);
                group.appendChild(control);
            } else {
                original.parentElement.insertBefore(track, original);
            }

            // The app can flip the toggle itself (preset load, reset).
            new MutationObserver(() => {
                const on = original.classList.contains("on");
                if (track.tpxSetState && track.classList.contains("on") !== on) {
                    track.tpxSetState(on);
                }
            }).observe(original, { attributes: true, attributeFilter: ["class"] });
        });
    }

    /* Selects and number inputs get the same one-line treatment — this
       is where most of the sidebar's vertical space goes. */
    function skinFormGroups() {
        document.querySelectorAll(".form-group").forEach((group) => {
            // Keep the left panel in native TPX form geometry. TP INPUT
            // is the requested reference: stacked label + full-width box.
            if (group.closest(".sidebar, .account-panel")) return;
            if (group.classList.contains("param-row")) return;
            const label = group.querySelector(":scope > label");
            const control = group.querySelector(
                ":scope > select, :scope > input[type=number], :scope > input[type=text]"
            );
            if (!label || !control) return;
            // Multi-control groups (help text, hint rows) keep stacking.
            if (group.querySelectorAll(":scope > select, :scope > input").length > 1) return;
            group.classList.add("param-row");
            label.classList.add("param-label");
            const wrap = el("span", "param-control");
            control.parentElement.insertBefore(wrap, control);
            wrap.appendChild(control);
        });
    }

    /* ── ideal #5: range inputs -> fluid sliders ───────────────────── */
    function skinRanges() {
        document.querySelectorAll('input[type="range"]').forEach((input) => {
            if (input.closest(".glass-slider")) return;
            if (!input.id) input.id = `rg-${Math.random().toString(36).slice(2, 8)}`;
            const min = parseFloat(input.min || "0");
            const max = parseFloat(input.max || "1");
            const step = parseFloat(input.step || "0");
            const value = parseFloat(input.value || String(min));
            const span = (max - min) || 1;

            const root = el("div", "glass-slider");
            root.dataset.stage = "slider";
            root.dataset.sliderProxy = input.id;
            root.dataset.min = String(min);
            root.dataset.max = String(max);
            root.dataset.step = String(step);
            root.dataset.value = String((value - min) / span);
            root.dataset.decimals = String((input.step || "").split(".")[1]?.length || 2);
            const track = el("div", "slider-track");
            track.appendChild(el("div", "slider-fill"));
            root.appendChild(track);
            root.appendChild(opticalSpan("slider-thumb", "slider"));

            input.classList.add("glass-proxy-hidden");
            input.parentElement.insertBefore(root, input);
        });
    }

    /* ── ideal #4: chart corner buttons -> layered FAB ─────────────── */
    function skinChartButtons() {
        const quick = byId("chart-quick-btns");
        if (!quick) return;
        const buttons = [...quick.querySelectorAll("button")];
        quick.classList.add("glass-fab");
        quick.removeAttribute("id");
        quick.id = "chart-quick-btns";
        buttons.forEach((button, index) => {
            button.classList.remove("chart-sq-btn");
            button.classList.add("optical-surface", "fab-action");
            button.dataset.optical = "fab";
            button.dataset.fabAction = String(index);
            if (button.id === "btn-auto-center") button.dataset.fabLatch = "1";
            const content = el("span", "surface-content");
            content.textContent = button.textContent.trim();
            button.textContent = "";
            button.appendChild(content);
        });
        const trigger = el("button", "optical-surface fab-button", {
            type: "button", "aria-expanded": "false", "aria-label": "Chart tools",
        });
        trigger.dataset.optical = "fab";
        const label = el("span", "surface-content");
        label.textContent = "≡";
        trigger.appendChild(label);
        quick.appendChild(trigger);
    }

    function markSidebarStage() {
        const sidebar = document.querySelector(".sidebar");
        if (sidebar) sidebar.dataset.stage = "side";
    }

    /* lightweight-charts paints its own background onto canvas, so no
       amount of CSS reaches it — a light-mode page kept a black chart.
       ancserTPX.js declares `let chart` at the top level of a classic
       script, which puts it in the shared global lexical scope: another
       classic script can read it directly (it is NOT on window).

       The chart is built only after data arrives, so poll briefly
       rather than assuming it exists at skin time. */
    function skinChartTheme() {
        const paint = () => {
            let instance = null;
            try { instance = chart; } catch (e) { return false; }
            if (!instance) return false;
            const light = document.documentElement.dataset.theme === "light";
            const chartBackground = getComputedStyle(document.documentElement)
                .getPropertyValue("--chart-bg").trim()
                || (light ? "#f7efe0" : "#08090d");
            const line = light
                ? "rgba(20, 50, 70, 0.10)"
                : "rgba(100, 220, 255, 0.08)";
            instance.applyOptions({
                layout: {
                    background: {
                        type: "solid",
                        color: chartBackground,
                    },
                    textColor: light ? "#5b6474" : "#556178",
                },
                grid: { vertLines: { color: line }, horzLines: { color: line } },
                rightPriceScale: { borderColor: light ? "rgba(20,50,70,0.18)" : "#1a1e2a" },
                timeScale: { borderColor: light ? "rgba(20,50,70,0.18)" : "#1a1e2a" },
            });
            return true;
        };

        let tries = 0;
        const poll = window.setInterval(() => {
            tries += 1;
            if (paint() || tries > 40) window.clearInterval(poll);
        }, 250);

        new MutationObserver(paint).observe(document.documentElement, {
            attributes: true, attributeFilter: ["data-theme"],
        });
    }

    function skin() {
        markSidebarStage();
        skinTopBar();
        skinBottomTabs();
        skinToggles();
        skinFormGroups();
        skinRanges();
        skinChartButtons();
        skinChartTheme();
        document.documentElement.dataset.tpxGlassSkin = "on";
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", skin, { once: true });
    } else {
        skin();
    }
})();
