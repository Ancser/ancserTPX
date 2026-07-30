/* ============================================================
   ancserTPX — liquid glass tuner (development tool)

   Port of the tuner sidebar from ancserAPX's
   frontend/static/demos/liquid-glass-component-gallery.html, re-hosted
   as a dropdown under the avatar in the top bar so each component type
   can be adjusted against the real UI instead of a showcase stage.

   The control table below (ranges, steps, value formatting) is the
   demo's `controlDefinitions` verbatim — same knobs, same limits — with
   one deliberate change: Center shrink goes to -0.6 rather than 0,
   because negative shrink means magnify (see createShrinkMap).

   HOW IT WRITES
     window.TpxGlass.settings[component] is the live tuning object the
     engine reads. This panel mutates it and then calls
     TpxGlass.retune(component), which persists to localStorage and
     rebuilds that component's filter chain.

   MAKING A CHANGE PERMANENT
     Tuning lives in localStorage — it survives reload but is per
     browser and invisible to anyone else. Press COPY to get the changed
     fields as JSON and paste them into the `defaults` table in
     tpx-glass.js. RESET drops back to those shipped values.

   This file is a dev tool: remove the <script> tag from
   ancserTPX.html and nothing else changes.
   ============================================================ */
(() => {
    "use strict";

    // Verbatim from the gallery demo, except the shrink lower bound.
    const CONTROLS = [
        { group: "Optics", key: "profile", label: "Profile", type: "select", options: [
            ["convex-circle", "Convex Circle"],
            ["convex-squircle", "Convex Squircle"],
            ["concave", "Concave"],
            ["lip", "Lip"],
        ] },
        { group: "Optics", key: "bezel", label: "Bezel", min: 2, max: 42, step: 1, format: (v) => `${v}px` },
        { group: "Optics", key: "refraction", label: "Refraction", min: 0, max: 2.2, step: 0.01, format: (v) => `${Number(v).toFixed(2)}x` },
        { group: "Optics", key: "thickness", label: "Thickness", min: 10, max: 220, step: 1, format: (v) => `${v}px` },
        // negative = magnify; the demo stopped at 0 (shrink only)
        { group: "Optics", key: "shrink", label: "Center shrink", min: -0.6, max: 0.6, step: 0.01, format: (v) => `${Math.round(Number(v) * 100)}%` },
        { group: "Optics", key: "specular", label: "Specular", min: 0, max: 0.6, step: 0.01, format: (v) => Number(v).toFixed(2) },
        { group: "Optics", key: "blur", label: "Blur", min: 0, max: 2, step: 0.01, format: (v) => `${Number(v).toFixed(2)}px` },
        { group: "Optics", key: "saturation", label: "Saturation", min: 0.5, max: 3, step: 0.01, format: (v) => `${Number(v).toFixed(2)}x` },
        { group: "Motion", key: "idleScale", label: "Idle scale", min: 0.45, max: 1.2, step: 0.01, format: (v) => Number(v).toFixed(2) },
        { group: "Motion", key: "activeScale", label: "Active scale", min: 0.6, max: 2, step: 0.01, format: (v) => Number(v).toFixed(2) },
        { group: "Motion", key: "stiffness", label: "Stiffness", min: 120, max: 1400, step: 10, format: (v) => String(v) },
        { group: "Motion", key: "damping", label: "Damping", min: 10, max: 90, step: 1, format: (v) => String(v) },
        { group: "Motion", key: "stretch", label: "Stretch", min: 0, max: 0.4, step: 0.01, format: (v) => Number(v).toFixed(2) },
    ];

    // Demo's `names`, trimmed to the components TPX actually mounts.
    const NAMES = {
        dock: "Top dock · pill",
        dockContainer: "Top dock · container",
        segment: "Bottom segment · pill",
        segmentContainer: "Bottom segment · container",
        slider: "Fluid slider · thumb",
        switch: "Tactile switch · thumb",
        fab: "Chart FAB",
        precision: "Chart pointer lens",
    };

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
        return node;
    }

    function build() {
        const glass = window.TpxGlass;
        const host = document.querySelector(".topbar-right");
        if (!glass || !host) return;

        /* Only offer components that actually mounted a surface — the
           list otherwise implies knobs that control nothing. */
        const mounted = glass.diagnostics.components || {};
        const keys = Object.keys(NAMES).filter((k) => mounted[k] && glass.settings[k]);
        if (!keys.length) return;

        let selected = keys.includes("dock") ? "dock" : keys[0];

        const wrap = el("div", "glass-tuner");
        const trigger = el("button", "tuner-trigger", {
            type: "button", title: "Tune glass optics", "aria-expanded": "false",
        });
        trigger.textContent = "◐";

        const panel = el("div", "tuner-panel");

        // Header: component picker + actions
        const head = el("div", "tuner-head");
        const picker = el("select", "tuner-picker");
        keys.forEach((key) => {
            const option = el("option");
            option.value = key;
            option.textContent = NAMES[key];
            picker.appendChild(option);
        });
        picker.value = selected;
        head.appendChild(picker);

        const actions = el("div", "tuner-actions");
        const resetBtn = el("button", "tuner-btn", { type: "button" });
        resetBtn.textContent = "RESET";
        const copyBtn = el("button", "tuner-btn", { type: "button" });
        copyBtn.textContent = "COPY";
        actions.appendChild(resetBtn);
        actions.appendChild(copyBtn);
        head.appendChild(actions);
        panel.appendChild(head);

        const body = el("div", "tuner-body");
        panel.appendChild(body);

        // Baked map previews — the fastest way to see what a knob did.
        const kernel = el("div", "tuner-kernel");
        const dispImg = el("img", "tuner-map", { alt: "Displacement map" });
        const specImg = el("img", "tuner-map", { alt: "Specular map" });
        const kernelNote = el("span", "tuner-kernel-note");
        kernelNote.textContent = "displacement / specular";
        kernel.appendChild(dispImg);
        kernel.appendChild(specImg);
        kernel.appendChild(kernelNote);
        panel.appendChild(kernel);

        const status = el("div", "tuner-status");
        panel.appendChild(status);

        const rows = new Map();

        function refreshKernel() {
            const maps = glass.kernel(selected);
            const has = Boolean(maps);
            kernel.classList.toggle("is-empty", !has);
            if (!has) return;
            dispImg.src = maps.displacement;
            specImg.src = maps.specular;
        }

        function markModified() {
            const base = glass.defaults[selected] || {};
            const live = glass.settings[selected];
            let changed = 0;
            rows.forEach((row, key) => {
                const dirty = base[key] !== live[key];
                row.wrap.classList.toggle("is-modified", dirty);
                if (dirty) changed += 1;
            });
            status.textContent = changed
                ? `${changed} field${changed > 1 ? "s" : ""} changed · COPY to keep`
                : "matching shipped defaults";
            status.classList.toggle("is-dirty", changed > 0);
        }

        function commit(key, raw) {
            const def = CONTROLS.find((c) => c.key === key);
            const value = def.type === "select" ? raw : Number(raw);
            glass.settings[selected][key] = value;
            const row = rows.get(key);
            if (row && row.out) row.out.textContent = def.format(value);
            glass.retune(selected);
            markModified();
            // Maps are rebuilt on the engine's next tick; read them after it.
            window.setTimeout(refreshKernel, 80);
        }

        function buildRows() {
            body.textContent = "";
            rows.clear();
            let currentGroup = null;
            CONTROLS.forEach((def) => {
                if (def.group !== currentGroup) {
                    currentGroup = def.group;
                    const title = el("div", "tuner-group");
                    title.textContent = def.group;
                    body.appendChild(title);
                }
                const row = el("label", "tuner-row");
                const label = el("span", "tuner-label");
                label.textContent = def.label;
                row.appendChild(label);

                let out = null;
                let input;
                if (def.type === "select") {
                    input = el("select", "tuner-input");
                    def.options.forEach(([value, text]) => {
                        const option = el("option");
                        option.value = value;
                        option.textContent = text;
                        input.appendChild(option);
                    });
                    input.value = glass.settings[selected][def.key];
                    input.addEventListener("change", () => commit(def.key, input.value));
                    row.appendChild(input);
                } else {
                    out = el("output", "tuner-out");
                    out.textContent = def.format(glass.settings[selected][def.key]);
                    label.appendChild(out);
                    input = el("input", "tuner-range", {
                        type: "range",
                        min: String(def.min),
                        max: String(def.max),
                        step: String(def.step),
                    });
                    input.value = String(glass.settings[selected][def.key]);
                    input.addEventListener("input", () => commit(def.key, input.value));
                    row.appendChild(input);
                }
                rows.set(def.key, { wrap: row, input, out });
                body.appendChild(row);
            });
            markModified();
            refreshKernel();
        }

        picker.addEventListener("change", () => {
            selected = picker.value;
            buildRows();
        });

        resetBtn.addEventListener("click", () => {
            glass.resetTuning(selected);
            buildRows();
            window.setTimeout(refreshKernel, 80);
        });

        copyBtn.addEventListener("click", async () => {
            const json = glass.exportTuning();
            const previous = copyBtn.textContent;
            try {
                await navigator.clipboard.writeText(json);
                copyBtn.textContent = "COPIED";
            } catch (error) {
                // Clipboard needs a secure context; fall back to the log.
                console.log("[TPX Glass] tuning:\n" + json);
                copyBtn.textContent = "IN CONSOLE";
            }
            window.setTimeout(() => { copyBtn.textContent = previous; }, 1400);
        });

        trigger.addEventListener("click", (event) => {
            event.stopPropagation();
            const open = wrap.classList.toggle("open");
            trigger.setAttribute("aria-expanded", String(open));
            if (open) refreshKernel();
        });

        // Clicks inside must not close it; outside should.
        panel.addEventListener("click", (event) => event.stopPropagation());
        document.addEventListener("click", (event) => {
            if (!wrap.contains(event.target)) wrap.classList.remove("open");
        });

        wrap.appendChild(trigger);
        wrap.appendChild(panel);
        host.appendChild(wrap);
        buildRows();

        window.TpxGlassTuner = { open: () => wrap.classList.add("open") };
    }

    /* The skin builds .topbar-right and the engine mounts the surfaces,
       both on DOMContentLoaded. This script runs after them, but the
       mounted-surface list is what decides which components to offer, so
       wait until it is actually populated.

       Polled on a timer rather than requestAnimationFrame: rAF does not
       fire while the tab is in the background, so an rAF-gated build
       silently never happened if the page finished loading unfocused —
       you would come back to a missing tuner button. */
    function start() {
        let tries = 0;
        const attempt = () => {
            tries += 1;
            const glass = window.TpxGlass;
            const ready = glass
                && document.querySelector(".topbar-right")
                && Object.keys(glass.diagnostics.components || {}).length > 0;
            if (ready) {
                build();
                return;
            }
            if (tries < 80) window.setTimeout(attempt, 100);
        };
        attempt();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
        start();
    }
})();
