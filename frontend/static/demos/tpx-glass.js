/* ============================================================
   ancserTPX — liquid glass engine (demo)

   TPX-first optics engine wired to the real ancserTPX stages and
   controls. The generated preview has no parallel mock interface.

   ── HOW IT RENDERS (read this before debugging anything) ─────────

   This is NOT backdrop-filter. backdrop-filter can blur what is
   behind an element but cannot *displace* it, and refraction is
   displacement. So the engine does this instead:

     1. Clone the whole stage ([data-stage]) into each glass surface.
     2. Transform the clone so it lines up pixel-for-pixel with the
        real stage underneath.
     3. Run an SVG filter chain over the clone:
          blur -> shrink displace -> refract displace -> saturate
          -> screen-blend a specular highlight
        where the displacement/specular maps are canvas-generated
        from a physically-derived refraction profile.

   ── THE CONSEQUENCE THAT MATTERS FOR TPX ─────────────────────────

   The surface refracts a DOM CLONE, not the live backdrop. A cloned
   <canvas> is blank. Production ancserTPX draws its chart into a
   <canvas>, so a glass dock floating over the real chart would
   refract empty space.

   TPX mirrors every live lightweight-charts canvas into its matching
   clone canvas at a bounded cadence. DOM-only stages use the same
   alignment path without the canvas cost.

   ── TUNING ───────────────────────────────────────────────────────

   The settings below are device-pixel and unitless quantities
   consumed by the filter chain; they are deliberately NOT rem-converted
   (see the sizing contract in tpx-glass.css).

   Geometry that *is* layout (track heights, thumb sizes, padding)
   lives in the CSS in rem, and is read back from getComputedStyle
   here rather than hardcoded, so the engine follows the root
   font-size instead of fighting it.
   ============================================================ */
(() => {
    "use strict";

    /* ── refraction profiles (verbatim) ──────────────────────────── */
    const profiles = {
        "convex-circle": (x) => Math.sqrt(1 - (1 - x) ** 2),
        "convex-squircle": (x) => (1 - (1 - x) ** 4) ** 0.25,
        "concave": (x) => 1 - Math.sqrt(1 - (1 - x) ** 2),
        "lip": (x) => {
            const smooth = x ** 3 * (x * (x * 6 - 15) + 10);
            const convex = Math.sqrt(1 - (1 - x) ** 2);
            return (1 - smooth) * convex + smooth * (1 - convex);
        },
    };

    /* ── component tuning (verbatim from the gallery demo) ───────── */
    const defaults = {
        slider:           { profile: "convex-squircle", bezel: 16, refraction: 0.85, thickness: 80, shrink: 0.00, specular: 0.14, blur: 0.10, saturation: 1.25, idleScale: 1.00, activeScale: 1.08, stiffness: 900, damping: 54, stretch: 0.17 },
        switch:           { profile: "convex-squircle", bezel: 19, refraction: 0.90, thickness: 47, shrink: 0.30, specular: 0.14, blur: 0.18, saturation: 1.25, idleScale: 1.00, activeScale: 1.50, stiffness: 820, damping: 48, stretch: 0.12 },
        dock:             { profile: "convex-squircle", bezel: 18, refraction: 0.92, thickness: 70, shrink: 0.30, specular: 0.15, blur: 0.20, saturation: 1.22, idleScale: 1.10, activeScale: 1.50, stiffness: 520, damping: 34, stretch: 0.16 },
        dockContainer:    { profile: "convex-squircle", bezel: 18, refraction: 0.92, thickness: 70, shrink: 0.30, specular: 0.15, blur: 0.20, saturation: 1.22, idleScale: 1.00, activeScale: 1.00, stiffness: 520, damping: 34, stretch: 0.00 },
        segment:          { profile: "convex-squircle", bezel: 18, refraction: 0.88, thickness: 68, shrink: 0.30, specular: 0.15, blur: 0.18, saturation: 1.25, idleScale: 0.94, activeScale: 1.50, stiffness: 760, damping: 42, stretch: 0.14 },
        segmentContainer: { profile: "convex-squircle", bezel: 18, refraction: 0.88, thickness: 68, shrink: 0.30, specular: 0.15, blur: 0.18, saturation: 1.25, idleScale: 1.00, activeScale: 1.00, stiffness: 760, damping: 42, stretch: 0.00 },
        fab:              { profile: "convex-squircle", bezel: 14, refraction: 0.86, thickness: 64, shrink: 0.30, specular: 0.16, blur: 0.16, saturation: 1.22, idleScale: 0.88, activeScale: 1.00, stiffness: 540, damping: 32, stretch: 0.18 },
        /* TPX precision lens:
             shrink -0.20  magnifies 20% (see createShrinkMap)
             blur     0.00  preserves sharp chart/text sampling. */
        precision:        { profile: "convex-squircle", bezel: 30, refraction: 1.50, thickness: 150, shrink: -0.20, specular: 0.16, blur: 0.00, saturation: 1.30, idleScale: 0.86, activeScale: 1.00, stiffness: 400, damping: 25, stretch: 0.15 },
    };

    /* Labels stay as a sharp layer ABOVE the lens instead of being cut
       out and re-rendered through it.

       A content-bypass pass can punch a lens-shaped hole in the real label
       (cutOriginalContentUnderLens) and paints a refracted copy in its
       place, via a second filter pass. On oversized showcase controls
       that can read as a nice optical touch; at ancserTPX's ~11px tab
       labels the displaced copy is just
       mushy, which is what the smeared bottom-nav text was.

       Turning this off also drops one whole filter chain per dock and
       segment, so the crisp version is the cheaper one too. */
    const CRISP_LABELS = true;

    const settings = Object.fromEntries(
        Object.entries(defaults).map(([key, value]) => [key, { ...value }])
    );

    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const byId = (id) => document.getElementById(id);
    const surfaces = [];
    let pendingFilterFrame = 0;
    let pendingSyncFrame = 0;
    const activeSpringLoops = new Set();
    let filterSeq = 0;
    let scrollSeq = 0;
    let sliderSeq = 0;
    let switchSeq = 0;

    /* Layout constants live in CSS (rem). Read them back so the
       engine tracks the root font-size instead of assuming 16px.
       The demo hardcoded 7 / 5 / 4 px here. */
    const padOf = (element) =>
        parseFloat(getComputedStyle(element).paddingLeft) || 0;

    /* ── spring (verbatim) ───────────────────────────────────────── */
    class Spring {
        constructor(value, stiffness = 500, damping = 32) {
            this.value = value;
            this.target = value;
            this.velocity = 0;
            this.stiffness = stiffness;
            this.damping = damping;
        }

        update(dt, config) {
            this.stiffness = config.stiffness;
            this.damping = config.damping;
            const force = (this.target - this.value) * this.stiffness;
            this.velocity += (force - this.velocity * this.damping) * dt;
            this.value += this.velocity * dt;
            return this.value;
        }

        settled(epsilon = 0.001) {
            return (
                Math.abs(this.target - this.value) < epsilon
                && Math.abs(this.velocity) < epsilon
            );
        }
    }

    function fastReturn(spring, target, retain = 0.38) {
        spring.value = target + (spring.value - target) * retain;
        spring.target = target;
        spring.velocity *= 0.18;
    }

    /* Punches a lens-shaped hole in the real (unrefracted) label text
       so the glass shows its own refracted copy instead of doubling. */
    function cutOriginalContentUnderLens(contentLayers, lens, active) {
        if (!active) {
            contentLayers.forEach((content) => {
                content.style.clipPath = "";
                content.style.webkitClipPath = "";
            });
            return;
        }

        const lensRect = lens.getBoundingClientRect();
        contentLayers.forEach((content) => {
            const rect = content.getBoundingClientRect();
            const seam = 0.75;
            const left = lensRect.left - rect.left - seam;
            const top = lensRect.top - rect.top - seam;
            const right = lensRect.right - rect.left + seam;
            const bottom = lensRect.bottom - rect.top + seam;
            const width = Math.max(0, right - left);
            const height = Math.max(0, bottom - top);
            const radius = Math.min(width, height) / 2;
            const number = (value) => Math.round(value * 100) / 100;
            const outer = [
                "M 0 0",
                `H ${number(rect.width)}`,
                `V ${number(rect.height)}`,
                "H 0 Z",
            ].join(" ");
            const hole = [
                `M ${number(left + radius)} ${number(top)}`,
                `H ${number(right - radius)}`,
                `A ${number(radius)} ${number(radius)} 0 0 1 ${number(right)} ${number(top + radius)}`,
                `V ${number(bottom - radius)}`,
                `A ${number(radius)} ${number(radius)} 0 0 1 ${number(right - radius)} ${number(bottom)}`,
                `H ${number(left + radius)}`,
                `A ${number(radius)} ${number(radius)} 0 0 1 ${number(left)} ${number(bottom - radius)}`,
                `V ${number(top + radius)}`,
                `A ${number(radius)} ${number(radius)} 0 0 1 ${number(left + radius)} ${number(top)}`,
                "Z",
            ].join(" ");
            const clip = `path(evenodd, "${outer} ${hole}")`;
            content.style.clipPath = clip;
            content.style.webkitClipPath = clip;
        });
    }

    /* ── map generation (verbatim) ───────────────────────────────── */

    function physicalProfile(config, bezelWidth, sampleCount = 256) {
        const surface = profiles[config.profile] || profiles["convex-squircle"];
        const eta = 1 / 1.5;
        const values = [];
        for (let i = 0; i < sampleCount; i += 1) {
            const x = i / sampleCount;
            const y = surface(x);
            const dx = x < 1 ? 0.0001 : -0.0001;
            const derivative = (surface(clamp(x + dx, 0, 1)) - y) / dx;
            const magnitude = Math.hypot(derivative, 1) || 1;
            const normalX = -derivative / magnitude;
            const normalY = -1 / magnitude;
            const dot = normalY;
            const k = 1 - eta * eta * (1 - dot * dot);
            if (k < 0) {
                values.push(0);
                continue;
            }
            const root = Math.sqrt(k);
            const refractedX = -(eta * dot + root) * normalX;
            const refractedY = eta - (eta * dot + root) * normalY;
            values.push(
                Math.abs(refractedY) < 1e-6
                    ? 0
                    : refractedX * ((y * bezelWidth + config.thickness) / refractedY)
            );
        }
        return values;
    }

    function capsuleCoordinate(value, size, radius) {
        const body = size - radius * 2;
        if (value < radius) return value - radius;
        if (value >= size - radius) return value - radius - body;
        return 0;
    }

    function createDisplacementMap(config, width, height, radius) {
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        const image = context.createImageData(width, height);
        const data = image.data;
        const safeRadius = clamp(radius, 2, Math.min(width, height) / 2 - 1);
        const bezel = clamp(config.bezel, 2, Math.max(2, safeRadius - 2));
        const profile = physicalProfile(config, bezel);
        const maximum = Math.max(1e-6, ...profile.map((v) => Math.abs(v)));
        const outerSquared = (safeRadius + 1) ** 2;
        const radiusSquared = safeRadius ** 2;
        const innerSquared = Math.max(0, safeRadius - bezel) ** 2;

        for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
                const offset = (y * width + x) * 4;
                data[offset] = 128;
                data[offset + 1] = 128;
                data[offset + 2] = 128;
                data[offset + 3] = 255;
                const cx = capsuleCoordinate(x, width, safeRadius);
                const cy = capsuleCoordinate(y, height, safeRadius);
                const distanceSquared = cx * cx + cy * cy;
                if (distanceSquared > outerSquared || distanceSquared < innerSquared) {
                    continue;
                }
                const distance = Math.sqrt(distanceSquared);
                const alpha = distanceSquared < radiusSquared
                    ? 1
                    : 1 - (distance - safeRadius)
                        / (Math.sqrt(outerSquared) - safeRadius);
                const index = Math.floor(
                    clamp((safeRadius - distance) / bezel, 0, 1) * profile.length
                );
                const displacement = profile[clamp(index, 0, profile.length - 1)] || 0;
                const nx = distance > 0 ? -cx / distance : 0;
                const ny = distance > 0 ? -cy / distance : 0;
                data[offset] = Math.round(clamp(
                    128 + nx * (displacement / maximum) * 127 * alpha, 0, 255
                ));
                data[offset + 1] = Math.round(clamp(
                    128 + ny * (displacement / maximum) * 127 * alpha, 0, 255
                ));
            }
        }
        context.putImageData(image, 0, 0);
        return { url: canvas.toDataURL("image/png"), maximum, bezel };
    }

    function createShrinkMap(config, width, height) {
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        const image = context.createImageData(width, height);
        const data = image.data;
        /* Yes — negative shrink magnifies.

           zoomOut > 0 displaces outward from the centre, so the copy
           reads as pushed away (shrunk). Flip the sign and it displaces
           inward, which magnifies. The gallery clamped to [0, 0.8] so
           only shrink was reachable; the range is now symmetric.

           `maximum` must be taken on the absolute value — it is the
           normalisation divisor for the map, and with a negative
           zoomOut the old Math.max(1e-6, ...) collapsed to 1e-6 and the
           displacement channels saturated. */
        const shrink = clamp(config.shrink || 0, -0.8, 0.8);
        const zoomOut = shrink !== 0 ? 1 / (1 - shrink) - 1 : 0;
        const maximum = Math.max(
            1e-6,
            Math.abs(width * 0.5 * zoomOut),
            Math.abs(height * 0.5 * zoomOut)
        );

        for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
                const offset = (y * width + x) * 4;
                const dx = (x - width / 2) * zoomOut;
                const dy = (y - height / 2) * zoomOut;
                data[offset] = Math.round(clamp(128 + dx / maximum * 127, 0, 255));
                data[offset + 1] = Math.round(clamp(128 + dy / maximum * 127, 0, 255));
                data[offset + 2] = 128;
                data[offset + 3] = 255;
            }
        }
        context.putImageData(image, 0, 0);
        return {
            url: canvas.toDataURL("image/png"),
            scale: shrink !== 0 ? maximum * 2 : 0,
        };
    }

    function createSpecularMap(config, width, height, radius) {
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        const image = context.createImageData(width, height);
        const data = image.data;
        const safeRadius = clamp(radius, 2, Math.min(width, height) / 2 - 1);
        const outerSquared = (safeRadius + 1) ** 2;
        const innerSquared = Math.max(0, safeRadius - 1.8) ** 2;
        const lightX = Math.cos(-Math.PI * 0.72);
        const lightY = Math.sin(-Math.PI * 0.72);

        for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
                const cx = capsuleCoordinate(x, width, safeRadius);
                const cy = capsuleCoordinate(y, height, safeRadius);
                const squared = cx * cx + cy * cy;
                if (squared > outerSquared || squared < innerSquared) continue;
                const distance = Math.sqrt(squared);
                const nx = distance > 0 ? cx / distance : 0;
                const ny = distance > 0 ? -cy / distance : 0;
                const dot = Math.abs(nx * lightX + ny * lightY);
                const edge = clamp((safeRadius - distance) / 1.8, 0, 1);
                const curve = dot * Math.sqrt(1 - (1 - edge) ** 2);
                const channel = Math.round(clamp(255 * curve, 0, 255));
                const offset = (y * width + x) * 4;
                data[offset] = channel;
                data[offset + 1] = channel;
                data[offset + 2] = channel;
                data[offset + 3] = Math.round(channel * curve);
            }
        }
        context.putImageData(image, 0, 0);
        return canvas.toDataURL("image/png");
    }

    function roundedRectPath(context, x, y, width, height, radius) {
        const r = clamp(radius, 0, Math.min(width, height) / 2);
        context.beginPath();
        context.moveTo(x + r, y);
        context.lineTo(x + width - r, y);
        context.quadraticCurveTo(x + width, y, x + width, y + r);
        context.lineTo(x + width, y + height - r);
        context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
        context.lineTo(x + r, y + height);
        context.quadraticCurveTo(x, y + height, x, y + height - r);
        context.lineTo(x, y + r);
        context.quadraticCurveTo(x, y, x + r, y);
        context.closePath();
    }

    function createContainerMaterialMap(element, width, height, radius) {
        const materialCanvas = document.createElement("canvas");
        const maskCanvas = document.createElement("canvas");
        materialCanvas.width = maskCanvas.width = width;
        materialCanvas.height = maskCanvas.height = height;
        const context = materialCanvas.getContext("2d");
        const maskContext = maskCanvas.getContext("2d");
        const shellStyle = getComputedStyle(element);
        const inset = 1;
        roundedRectPath(
            context, inset, inset,
            width - inset * 2, height - inset * 2,
            Math.max(0, radius - inset)
        );
        context.lineWidth = 2;
        context.strokeStyle = shellStyle.borderTopColor;
        context.stroke();
        roundedRectPath(
            maskContext, inset, inset,
            width - inset * 2, height - inset * 2,
            Math.max(0, radius - inset)
        );
        maskContext.fillStyle = "#fff";
        maskContext.fill();
        return {
            material: materialCanvas.toDataURL("image/png"),
            mask: maskCanvas.toDataURL("image/png"),
        };
    }

    /* ── filter construction (verbatim) ──────────────────────────── */

    function createOpticalFilter(id, parentPass = false) {
        const ns = "http://www.w3.org/2000/svg";
        const filter = document.createElementNS(ns, "filter");
        filter.setAttribute("id", id);
        filter.setAttribute("x", "-100%");
        filter.setAttribute("y", "-100%");
        filter.setAttribute("width", "300%");
        filter.setAttribute("height", "300%");
        filter.setAttribute("color-interpolation-filters", "sRGB");
        const parentPipeline = parentPass
            ? `
                <feGaussianBlur data-node="parent-blur" in="SourceGraphic" stdDeviation="0" result="parentBlurred"></feGaussianBlur>
                <feImage data-node="parent-shrink-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="parentShrinkMap"></feImage>
                <feDisplacementMap data-node="parent-shrink-displacement" in="parentBlurred" in2="parentShrinkMap" scale="0" xChannelSelector="R" yChannelSelector="G" result="parentShrunk"></feDisplacementMap>
                <feImage data-node="parent-displacement-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="parentDisplacementMap"></feImage>
                <feDisplacementMap data-node="parent-displacement" in="parentShrunk" in2="parentDisplacementMap" scale="0" xChannelSelector="R" yChannelSelector="G" result="parentRefracted"></feDisplacementMap>
                <feColorMatrix data-node="parent-saturation" in="parentRefracted" type="saturate" values="1" result="parentSaturated"></feColorMatrix>
                <feImage data-node="parent-specular-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="parentSpecularMap"></feImage>
                <feComponentTransfer in="parentSpecularMap" result="parentSpecularFaded">
                    <feFuncA data-node="parent-specular-alpha" type="linear" slope="0"></feFuncA>
                </feComponentTransfer>
                <feBlend in="parentSpecularFaded" in2="parentSaturated" mode="screen" result="parentGlass"></feBlend>
                <feImage data-node="parent-mask-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="parentMaskMap"></feImage>
                <feComposite in="parentGlass" in2="parentMaskMap" operator="in" result="parentGlassClipped"></feComposite>
                <feImage data-node="parent-material-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="parentMaterialMap"></feImage>
                <feBlend in="parentMaterialMap" in2="parentGlassClipped" mode="normal" result="parentSurface"></feBlend>
                <feBlend in="parentSurface" in2="SourceGraphic" mode="normal" result="parentComposite"></feBlend>
            `
            : "";
        const sourceInput = parentPass ? "parentComposite" : "SourceGraphic";
        const nestedShrinkResolver = parentPass
            ? `
                <feComposite in="parentComposite" in2="parentMaskMap" operator="in" result="parentStableInside"></feComposite>
                <feComposite in="shrunk" in2="parentMaskMap" operator="out" result="childShrunkOutsideParent"></feComposite>
                <feMerge result="nestedShrinkResolved">
                    <feMergeNode in="childShrunkOutsideParent"></feMergeNode>
                    <feMergeNode in="parentStableInside"></feMergeNode>
                </feMerge>
            `
            : "";
        const displacementInput = parentPass ? "nestedShrinkResolved" : "shrunk";
        filter.innerHTML = `
            ${parentPipeline}
            <feGaussianBlur data-node="blur" in="${sourceInput}" stdDeviation="0.2" result="blurred"></feGaussianBlur>
            <feImage data-node="shrink-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="shrinkMap"></feImage>
            <feDisplacementMap data-node="shrink-displacement" in="blurred" in2="shrinkMap" scale="0" xChannelSelector="R" yChannelSelector="G" result="shrunk"></feDisplacementMap>
            ${nestedShrinkResolver}
            <feImage data-node="displacement-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="displacementMap"></feImage>
            <feDisplacementMap data-node="displacement" in="${displacementInput}" in2="displacementMap" scale="50" xChannelSelector="R" yChannelSelector="G" result="refracted"></feDisplacementMap>
            <feColorMatrix data-node="saturation" in="refracted" type="saturate" values="1.3" result="refractedSaturated"></feColorMatrix>
            <feImage data-node="specular-image" x="0" y="0" width="100" height="60" preserveAspectRatio="none" result="specularMap"></feImage>
            <feComponentTransfer in="specularMap" result="specularFaded">
                <feFuncA data-node="specular-alpha" type="linear" slope="0.15"></feFuncA>
            </feComponentTransfer>
            <feBlend in="specularFaded" in2="refractedSaturated" mode="screen"></feBlend>
        `;
        byId("filterDefs").appendChild(filter);
        const pick = (name) => filter.querySelector(`[data-node="${name}"]`);
        const parent = parentPass
            ? {
                blur: pick("parent-blur"),
                shrinkImage: pick("parent-shrink-image"),
                shrinkDisplacement: pick("parent-shrink-displacement"),
                displacementImage: pick("parent-displacement-image"),
                displacement: pick("parent-displacement"),
                saturation: pick("parent-saturation"),
                specularImage: pick("parent-specular-image"),
                materialImage: pick("parent-material-image"),
                maskImage: pick("parent-mask-image"),
                specularAlpha: pick("parent-specular-alpha"),
            }
            : null;
        return {
            filter,
            parent,
            blur: pick("blur"),
            shrinkImage: pick("shrink-image"),
            shrinkDisplacement: pick("shrink-displacement"),
            displacementImage: pick("displacement-image"),
            displacement: pick("displacement"),
            saturation: pick("saturation"),
            specularImage: pick("specular-image"),
            specularAlpha: pick("specular-alpha"),
        };
    }

    function setHref(node, value) {
        node.setAttribute("href", value);
        node.setAttributeNS("http://www.w3.org/1999/xlink", "href", value);
    }

    function sanitizeClone(root) {
        root.removeAttribute("id");
        if (root.dataset.stage === "research") root.classList.remove("hidden");
        root.setAttribute("aria-hidden", "true");
        root.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
        root.querySelectorAll("button, input, select, [tabindex]").forEach((node) => {
            node.setAttribute("tabindex", "-1");
        });
    }

    function markScrollSources(stage) {
        [stage, ...stage.querySelectorAll("*")].forEach((node) => {
            if (node.closest(".optical-layer")) return;
            const style = getComputedStyle(node);
            const overflow = `${style.overflow} ${style.overflowX} ${style.overflowY}`;
            const scrollable = /(auto|scroll|overlay)/.test(overflow)
                && (node.scrollHeight > node.clientHeight
                    || node.scrollWidth > node.clientWidth);
            if (scrollable && !node.dataset.opticalScrollKey) {
                node.dataset.opticalScrollKey = `scroll-${scrollSeq++}`;
            }
        });
    }

    function mirrorNestedScrollState(sourceRoot, copyRoot) {
        if (!copyRoot) return;
        [sourceRoot, ...sourceRoot.querySelectorAll("[data-optical-scroll-key]")]
            .forEach((source) => {
            // Stage clones live inside the source stage's optical layers.
            // Never let their zero/default scroll state overwrite the real
            // scroller that was copied just before them.
            if (source !== sourceRoot && source.closest(".optical-layer")) return;
            const key = source.dataset.opticalScrollKey;
            if (!key) return;
            const copy = source === sourceRoot
                && copyRoot.dataset.opticalScrollKey === key
                ? copyRoot
                : copyRoot.querySelector(`[data-optical-scroll-key="${key}"]`);
            if (!copy) return;
            copy.scrollLeft = source.scrollLeft;
            copy.scrollTop = source.scrollTop;
        });
    }

    function markComponentSources() {
        liveAll(".glass-slider").forEach((root) => {
            if (!root.dataset.sliderKey) {
                root.dataset.sliderKey = `slider-${sliderSeq++}`;
            }
        });
        liveAll(".glass-switch").forEach((track) => {
            if (!track.dataset.switchKey) {
                track.dataset.switchKey = `switch-${switchSeq++}`;
            }
        });
    }

    /* ── surface wiring ──────────────────────────────────────────── */

    const stageTemplates = new Map();

    function buildOpticalSurfaces() {
        // Keys must exist before cloning, otherwise every component glass
        // falls back to the first slider/switch in the sampled stage.
        markComponentSources();
        const stages = Array.from(document.querySelectorAll("[data-stage]"));
        stages.forEach((stage) => {
            markScrollSources(stage);
            const copy = stage.cloneNode(true);
            sanitizeClone(copy);
            copy.classList.add("optical-stage-copy");
            stageTemplates.set(stage, copy);
        });

        Array.from(document.querySelectorAll("[data-optical]")).forEach((element) => {
            const component = element.dataset.optical;
            /* data-optical-stage lets a surface name its stage by
               selector instead of inheriting the nearest ancestor. The
               top bar needs it: it has to live outside .main so the
               Research tab's `mainEl.style.display='none'` cannot take
               the nav down with it, but it still refracts the chart. */
            const stageRef = element.closest("[data-optical-stage]");
            const stage = stageRef
                ? document.querySelector(stageRef.dataset.opticalStage)
                : element.closest("[data-stage]");
            if (!stage || !stageTemplates.has(stage)) return;
            const index = filterSeq++;
            const layer = document.createElement("span");
            const world = document.createElement("span");
            const isContainerGlass = element.hasAttribute("data-container-glass");
            const needsContentBypass =
                !CRISP_LABELS
                && !isContainerGlass
                && (component === "dock" || component === "segment");
            const filterNodes = createOpticalFilter(
                `tpx-optical-filter-${index}`, needsContentBypass
            );
            const stageCopy = stageTemplates.get(stage).cloneNode(true);
            const parentComponent = needsContentBypass ? `${component}Container` : null;
            const parentContainerSource = needsContentBypass
                ? element.parentElement.querySelector(`[data-optical="${parentComponent}"]`)
                : null;
            let contentLayer = null;
            let contentWorld = null;
            let contentStageCopy = null;
            let contentFilterNodes = null;
            layer.className = "optical-layer";
            layer.setAttribute("aria-hidden", "true");
            layer.style.filter = `url(#tpx-optical-filter-${index})`;
            world.className = "optical-world";
            if (isContainerGlass) stageCopy.classList.add("container-shell-copy");
            if (needsContentBypass) {
                stageCopy.classList.add("optical-background-copy", "container-shell-copy");
                contentLayer = document.createElement("span");
                contentWorld = document.createElement("span");
                contentStageCopy = stageTemplates.get(stage).cloneNode(true);
                contentFilterNodes = createOpticalFilter(`tpx-optical-content-filter-${index}`);
                contentLayer.className = "optical-layer optical-content-layer";
                contentLayer.setAttribute("aria-hidden", "true");
                contentLayer.style.filter = `url(#tpx-optical-content-filter-${index})`;
                contentWorld.className = "optical-world";
                contentStageCopy.classList.add("optical-content-copy");
                contentWorld.appendChild(contentStageCopy);
                contentLayer.appendChild(contentWorld);
            }
            world.appendChild(stageCopy);
            layer.appendChild(world);
            element.prepend(layer);
            if (contentLayer) element.prepend(contentLayer);
            surfaces.push({
                component, element, stage, layer, world, stageCopy, filterNodes,
                contentLayer, contentWorld, contentStageCopy, contentFilterNodes,
                parentComponent, parentContainerSource,
                /* Per-instance hook: mirrors live state (fill widths,
                   toggle classes) into the clone. The demo hardcoded
                   this by element id; TPX has many instances. */
                syncSample: null,
                lastMap: null,
                lastSpecular: null,
            });
        });

        surfaces.forEach(bindMirrors);

        const observer = new ResizeObserver(() => {
            scheduleOpticalSync();
            scheduleFilterRebuild();
        });
        stages.forEach((stage) => observer.observe(stage));
        syncOpticalSurfaces();
        rebuildFilters();
    }

    function logicalOffsetWithin(element, ancestor) {
        let x = 0;
        let y = 0;
        let node = element;
        while (node && node !== ancestor) {
            x += node.offsetLeft;
            y += node.offsetTop;
            node = node.offsetParent;
        }
        if (node === ancestor) return { x, y };
        const elementRect = element.getBoundingClientRect();
        const ancestorRect = ancestor.getBoundingClientRect();
        return {
            x: elementRect.left - ancestorRect.left + ancestor.scrollLeft,
            y: elementRect.top - ancestorRect.top + ancestor.scrollTop,
        };
    }

    function syncOpticalSurfaces(component = null) {
        pendingSyncFrame = 0;
        surfaces.forEach((surface) => {
            if (component && surface.component !== component) return;
            if (surface.syncSample) {
                surface.syncSample(surface.stageCopy);
                if (surface.contentStageCopy) surface.syncSample(surface.contentStageCopy);
            }
            // A surface rooted at .main can cross nested scrollers such
            // as the sidebar. Keep those clone scroll positions current.
            mirrorNestedScrollState(surface.stage, surface.stageCopy);
            mirrorNestedScrollState(surface.stage, surface.contentStageCopy);
            /* A surface can outlive the visibility of its stage: the top
               bar stays up while the Research tab does
               mainEl.style.display='none', and the clone it refracts
               collapses to zero size. Drop the lens rather than render a
               collapsed one — the CSS backdrop-filter underneath is the
               graceful fallback. */
            const stageVisible = surface.stage.offsetWidth > 0
                && surface.stage.offsetHeight > 0;
            if (surface.layer) surface.layer.style.display = stageVisible ? "" : "none";
            if (surface.contentLayer) {
                surface.contentLayer.style.display = stageVisible ? "" : "none";
            }
            if (!stageVisible) return;
            mirrorCanvases(surface);
            const elementRect = surface.element.getBoundingClientRect();
            const stageRect = surface.stage.getBoundingClientRect();
            const logical = logicalOffsetWithin(surface.element, surface.stage);
            const scaleX = Math.max(
                0.01, elementRect.width / Math.max(1, surface.element.offsetWidth)
            );
            const scaleY = Math.max(
                0.01, elementRect.height / Math.max(1, surface.element.offsetHeight)
            );
            const scaleInsetX = (surface.element.offsetWidth - elementRect.width) / 2;
            const scaleInsetY = (surface.element.offsetHeight - elementRect.height) / 2;
            const x = (logical.x + scaleInsetX) / scaleX;
            const y = (logical.y + scaleInsetY) / scaleY;
            /* Size the clone to the stage's SCROLL box, not its visible
               box.

               The offsets below are content-space (offsetLeft/offsetTop,
               which ignore scrolling), so the clone has to lay its
               content out at full length for them to line up. Sized to
               the visible box instead, a scrollable stage like .sidebar
               clips its clone at scrollTop 0 — so a control scrolled
               halfway down the panel refracted whatever happened to be
               at the top of the panel. That was the slider sampling the
               wrong place. */
            const stageW = Math.max(stageRect.width, surface.stage.scrollWidth);
            const stageH = Math.max(stageRect.height, surface.stage.scrollHeight);
            [
                [surface.world, surface.stageCopy],
                [surface.contentWorld, surface.contentStageCopy],
            ].forEach(([world, stageCopy]) => {
                if (!world || !stageCopy) return;
                world.style.width = `${stageW}px`;
                world.style.height = `${stageH}px`;
                stageCopy.style.width = `${stageW}px`;
                stageCopy.style.height = `${stageH}px`;
                world.style.transformOrigin = "0 0";
                world.style.transform =
                    `translate3d(${-x}px, ${-y}px, 0) scale(${1 / scaleX}, ${1 / scaleY})`;
            });
            if (surface.filterNodes.parent && surface.parentContainerSource) {
                const parentLogical = logicalOffsetWithin(
                    surface.parentContainerSource, surface.stage
                );
                const parentX = (parentLogical.x - logical.x - scaleInsetX) / scaleX;
                const parentY = (parentLogical.y - logical.y - scaleInsetY) / scaleY;
                const parentWidth = surface.parentContainerSource.offsetWidth / scaleX;
                const parentHeight = surface.parentContainerSource.offsetHeight / scaleY;
                [
                    surface.filterNodes.parent.shrinkImage,
                    surface.filterNodes.parent.displacementImage,
                    surface.filterNodes.parent.specularImage,
                    surface.filterNodes.parent.maskImage,
                    surface.filterNodes.parent.materialImage,
                ].forEach((node) => {
                    node.setAttribute("x", parentX.toFixed(3));
                    node.setAttribute("y", parentY.toFixed(3));
                    node.setAttribute("width", parentWidth.toFixed(3));
                    node.setAttribute("height", parentHeight.toFixed(3));
                });
            }
        });
    }

    function scheduleOpticalSync() {
        if (pendingSyncFrame) cancelAnimationFrame(pendingSyncFrame);
        pendingSyncFrame = requestAnimationFrame(() => syncOpticalSurfaces());
    }

    /* ── canvas mirroring ────────────────────────────────────────────
       cloneNode copies a <canvas> element but NOT its pixels, so any
       stage backed by a canvas refracts blank space. ancserTPX draws
       the chart with lightweight-charts (2D canvas) plus three overlay
       canvases, so without this the whole chart area is invisible to
       the glass.

       Fix: after each sync, drawImage() the live canvas into its
       counterpart in the clone. Cost is one full-surface blit per
       cloned canvas per frame, which is exactly the expense the CSS
       backdrop path avoids — so it is opt-out via setCanvasMirror(),
       and downscaled by mirrorScale (the copy is about to be blurred
       and displaced, so it does not need 1:1 pixels).

       Pairing is positional: querySelectorAll('canvas') returns tree
       order, and the clone is a structural copy, so index i maps to
       index i. If the counts diverge the app created a canvas after
       the clone was taken, and the templates are rebuilt. */
    const mirror = {
        enabled: true,
        scale: 0.5,
        /* Independent heartbeat, because the chart repaints on ticks /
           pan / zoom with no spring running. 15fps: fast enough that a
           glass panel over a live chart never looks frozen, slow enough
           that the blit cost stays off the interaction path. */
        intervalMs: 33,
        raf: 0,
        blits: 0,
        lastCost: 0,
        rebuildTimer: 0,
    };

    const liveCanvases = (root) =>
        [...root.querySelectorAll("canvas")].filter(
            (c) => !c.closest(".optical-stage-copy") && !c.closest(".optical-layer")
        );

    function bindMirrors(surface) {
        const sources = liveCanvases(surface.stage);
        surface.mirrorPairs = [];
        if (!sources.length) return;
        [surface.stageCopy, surface.contentStageCopy].forEach((copy) => {
            if (!copy) return;
            const targets = [...copy.querySelectorAll("canvas")];
            sources.forEach((src, i) => {
                if (targets[i]) surface.mirrorPairs.push([src, targets[i]]);
            });
        });
        surface.mirrorSourceCount = sources.length;
    }

    function scheduleStageCloneRebuild() {
        if (mirror.rebuildTimer) return;
        mirror.rebuildTimer = window.setTimeout(() => {
            mirror.rebuildTimer = 0;
            rebuildStageClones();
        }, 120);
    }

    function observeLiveStageContent() {
        const observer = new MutationObserver((mutations) => {
            const changed = mutations.some((mutation) => {
                const target = mutation.target.nodeType === 1
                    ? mutation.target
                    : mutation.target.parentElement;
                return target && !target.closest(".optical-layer");
            });
            if (changed) scheduleStageCloneRebuild();
        });
        stageTemplates.forEach((_, stage) => {
            if (!["chart", "bottom", "research"].includes(stage.dataset.stage)) {
                return;
            }
            observer.observe(stage, {
                childList: true,
                characterData: true,
                subtree: true,
            });
        });
    }

    /* The app adds canvases lazily (the volume-profile / fade-level /
       session-divider overlays are created on first use), so a clone
       taken at boot goes stale. Re-take the templates and swap the
       copies in place rather than rebuilding every filter. */
    function rebuildStageClones() {
        stageTemplates.forEach((_, stage) => {
            markScrollSources(stage);
            const fresh = stage.cloneNode(true);
            sanitizeClone(fresh);
            fresh.classList.add("optical-stage-copy");
            fresh.querySelectorAll(".optical-layer").forEach((n) => n.remove());
            stageTemplates.set(stage, fresh);
        });
        surfaces.forEach((surface) => {
            const template = stageTemplates.get(surface.stage);
            if (!template) return;
            [["stageCopy", "world"], ["contentStageCopy", "contentWorld"]]
                .forEach(([copyKey, worldKey]) => {
                    const old = surface[copyKey];
                    const world = surface[worldKey];
                    if (!old || !world) return;
                    const next = template.cloneNode(true);
                    next.className = old.className;
                    next.style.cssText = old.style.cssText;
                    world.replaceChild(next, old);
                    surface[copyKey] = next;
                });
            bindMirrors(surface);
        });
        scheduleOpticalSync();
    }

    function mirrorCanvases(surface) {
        if (!mirror.enabled || !surface.mirrorPairs) return;
        if (!surface.mirrorPairs.length) return;
        if (liveCanvases(surface.stage).length !== surface.mirrorSourceCount) {
            scheduleStageCloneRebuild();
            return;
        }
        const started = performance.now();
        surface.mirrorPairs.forEach(([src, dst]) => {
            if (!src.width || !src.height) return;
            const w = Math.max(1, Math.round(src.width * mirror.scale));
            const h = Math.max(1, Math.round(src.height * mirror.scale));
            if (dst.width !== w || dst.height !== h) {
                dst.width = w;
                dst.height = h;
            }
            const context = dst.getContext("2d");
            if (!context) return;
            context.clearRect(0, 0, w, h);
            try {
                context.drawImage(src, 0, 0, w, h);
                mirror.blits += 1;
            } catch (error) {
                /* Tainted canvas (cross-origin image drawn into the
                   chart) throws on read-back. Disable rather than
                   throw once per frame forever. */
                mirror.enabled = false;
            }
        });
        mirror.lastCost = performance.now() - started;
    }

    function rebuildSurface(surface) {
        const config = settings[surface.component];
        if (!config) return;
        const width = Math.max(2, Math.round(surface.element.offsetWidth));
        const height = Math.max(2, Math.round(surface.element.offsetHeight));
        const computed = getComputedStyle(surface.element);
        const radius = clamp(
            parseFloat(computed.borderTopLeftRadius) || height / 2,
            2, Math.min(width, height) / 2
        );
        const shrink = createShrinkMap(config, width, height);
        const displacement = createDisplacementMap(config, width, height, radius);
        const specular = createSpecularMap(config, width, height, radius);
        const configureNodes = (
            nodes, passConfig, passShrink, passDisplacement, passSpecular,
            passWidth, passHeight, shrinkScale, specularStrength
        ) => {
            if (!nodes) return;
            setHref(nodes.shrinkImage, passShrink.url);
            setHref(nodes.displacementImage, passDisplacement.url);
            setHref(nodes.specularImage, passSpecular);
            [nodes.shrinkImage, nodes.displacementImage, nodes.specularImage]
                .forEach((node) => {
                    node.setAttribute("width", String(passWidth));
                    node.setAttribute("height", String(passHeight));
                });
            nodes.shrinkDisplacement.setAttribute("scale", shrinkScale.toFixed(3));
            nodes.displacement.setAttribute(
                "scale",
                (passDisplacement.maximum * passConfig.refraction).toFixed(3)
            );
            nodes.blur.setAttribute("stdDeviation", passConfig.blur.toFixed(3));
            nodes.saturation.setAttribute("values", passConfig.saturation.toFixed(3));
            nodes.specularAlpha.setAttribute("slope", specularStrength.toFixed(3));
        };
        configureNodes(
            surface.filterNodes, config, shrink, displacement, specular,
            width, height, shrink.scale, config.specular
        );
        configureNodes(
            surface.contentFilterNodes, config, shrink, displacement, specular,
            width, height, 0, 0
        );
        if (
            surface.filterNodes.parent
            && surface.parentContainerSource
            && surface.parentComponent
        ) {
            const parentConfig = settings[surface.parentComponent];
            const parentWidth = Math.max(
                2, Math.round(surface.parentContainerSource.offsetWidth)
            );
            const parentHeight = Math.max(
                2, Math.round(surface.parentContainerSource.offsetHeight)
            );
            const parentComputed = getComputedStyle(surface.parentContainerSource);
            const parentRadius = clamp(
                parseFloat(parentComputed.borderTopLeftRadius) || parentHeight / 2,
                2, Math.min(parentWidth, parentHeight) / 2
            );
            const parentShrink = createShrinkMap(parentConfig, parentWidth, parentHeight);
            const parentDisplacement = createDisplacementMap(
                parentConfig, parentWidth, parentHeight, parentRadius
            );
            const parentSpecular = createSpecularMap(
                parentConfig, parentWidth, parentHeight, parentRadius
            );
            const parentMaterial = createContainerMaterialMap(
                surface.parentContainerSource, parentWidth, parentHeight, parentRadius
            );
            configureNodes(
                surface.filterNodes.parent, parentConfig, parentShrink,
                parentDisplacement, parentSpecular, parentWidth, parentHeight,
                parentShrink.scale, parentConfig.specular
            );
            setHref(surface.filterNodes.parent.materialImage, parentMaterial.material);
            setHref(surface.filterNodes.parent.maskImage, parentMaterial.mask);
        }
        surface.lastMap = displacement.url;
        surface.lastSpecular = specular;
    }

    function rebuildFilters(component = null) {
        pendingFilterFrame = 0;
        surfaces.forEach((surface) => {
            if (
                !component
                || surface.component === component
                || surface.parentComponent === component
            ) {
                rebuildSurface(surface);
            }
        });
        scheduleOpticalSync();
    }

    function scheduleFilterRebuild(component = null) {
        if (pendingFilterFrame) cancelAnimationFrame(pendingFilterFrame);
        pendingFilterFrame = requestAnimationFrame(() => rebuildFilters(component));
    }

    function runSpringLoop(
        component, springs, apply, shouldContinue = null, loopKey = component
    ) {
        if (activeSpringLoops.has(loopKey)) return;
        activeSpringLoops.add(loopKey);
        let previous = performance.now();
        const frame = (now) => {
            const dt = Math.min(0.032, Math.max(0.001, (now - previous) / 1000));
            previous = now;
            const config = settings[component];
            springs.forEach((spring) => spring.update(dt, config));
            apply();
            syncOpticalSurfaces(component);
            const moving = shouldContinue
                ? shouldContinue()
                : springs.some((spring) => !spring.settled());
            if (moving) requestAnimationFrame(frame);
            else activeSpringLoops.delete(loopKey);
        };
        requestAnimationFrame(frame);
    }

    const surfaceFor = (element) => surfaces.find((item) => item.element === element);

    /* ── clone guards ────────────────────────────────────────────────
       Every glass surface contains a full clone of its stage, so after
       buildOpticalSurfaces() the document holds several inert copies of
       the dock, the segment control, the sliders and the switches.

       Two ways that bites, both of which it did:

       - init()'s querySelectorAll matches the copies as well as the
         real controls, binding a second controller to dead DOM. The
         runSpringLoop guard is keyed by component name, so whichever
         controller claimed the loop first won and the others' apply()
         never ran.

       - Inside a controller, querySelector is not safe either. The
         container-glass span is the dock's FIRST child and carries a
         clone, so dock.querySelector('.dock-bubble') returned the
         clone's bubble — the real one was never positioned.

       These helpers keep every lookup on the live tree. */
    /* setPointerCapture throws NotFoundError if the pointer is already
       gone (fast flick, pointer released mid-handler, synthetic events
       from tests). The demo called it bare, so that throw aborted the
       rest of pointerdown — including the update() that actually moves
       the control — and the slider silently stopped responding. Never
       let capture failure take the interaction down with it. */
    function capturePointer(element, pointerId) {
        try {
            element.setPointerCapture?.(pointerId);
        } catch (error) {
            /* uncaptured drags still track via pointermove */
        }
    }

    function releasePointer(element, pointerId) {
        try {
            if (element.hasPointerCapture?.(pointerId)) {
                element.releasePointerCapture(pointerId);
            }
        } catch (error) {
            /* already released */
        }
    }

    const isClone = (element) => Boolean(element.closest(".optical-stage-copy"));
    const liveAll = (selector, root = document) =>
        [...root.querySelectorAll(selector)].filter((el) => !isClone(el));
    const live = (selector, root = document) => liveAll(selector, root)[0] || null;

    /* ── ideal #1: drag dock ─────────────────────────────────────── */

    function initDragDock(dock, onSelect) {
        const bubble = live(".dock-bubble", dock);
        const buttons = Array.from(dock.children)
            .filter((node) => node.matches("[data-dock-index]"));
        if (!bubble || !buttons.length) return;
        const contentLayers = buttons.map(
            (button) => button.querySelector(".control-source-content")
        );
        const x = new Spring(0);
        const scale = new Spring(settings.dock.idleScale);
        const stretch = new Spring(1);
        const activity = new Spring(0);
        let pointerActive = false;
        let dragging = false;
        let pointerId = null;
        let startClientX = 0;
        let ignoreClickUntil = 0;
        let selected = buttons.findIndex((b) => b.classList.contains("active"));
        if (selected < 0) selected = 0;
        let maskActive = false;
        let releaseTimer = 0;
        let returningFlat = false;
        const dragDistancePx = 6;
        const pad = () => padOf(dock);
        const cell = () => (dock.clientWidth - pad() * 2) / buttons.length;
        const targetFor = (index) => index * cell();
        const apply = () => {
            const moving = [x, scale, stretch].some((spring) => !spring.settled(0.002));
            const glassShapeActive = (dragging || moving) && !returningFlat;
            if (glassShapeActive) {
                activity.target = 1;
                maskActive = true;
            } else if (activity.target !== 0) {
                fastReturn(activity, 0, 0.14);
            }
            if (activity.target === 0 && activity.value <= 0.002) {
                activity.value = 0;
                activity.velocity = 0;
                maskActive = false;
            }
            const glassOpacity = clamp(activity.value, 0, 1);
            dock.classList.toggle("interacting", glassShapeActive);
            dock.style.setProperty("--control-glass", glassOpacity.toFixed(4));
            bubble.style.left = `${pad() + x.value}px`;
            bubble.style.width = `${cell()}px`;
            bubble.style.transform =
                `scale(${scale.value * stretch.value}, ${scale.value / stretch.value})`;
            if (!CRISP_LABELS) cutOriginalContentUnderLens(contentLayers, bubble, maskActive);
        };
        const start = () => runSpringLoop(
            "dock", [x, scale, stretch, activity], apply,
            () => dragging || [x, scale, stretch, activity].some((s) => !s.settled())
        );
        const beginIdleReturn = () => {
            returningFlat = true;
            fastReturn(activity, 0, 0.14);
            fastReturn(scale, settings.dock.idleScale);
            fastReturn(stretch, 1);
            start();
        };
        const pulse = () => {
            clearTimeout(releaseTimer);
            returningFlat = false;
            maskActive = true;
            activity.value = Math.max(activity.value, 0.18);
            activity.target = 1;
            scale.target = settings.dock.activeScale;
            start();
            releaseTimer = setTimeout(beginIdleReturn, 110);
        };
        const select = (index, animate = true) => {
            selected = clamp(index, 0, buttons.length - 1);
            x.target = targetFor(selected);
            buttons.forEach((button, i) => button.classList.toggle("active", i === selected));
            if (onSelect) onSelect(buttons[selected], selected);
            if (animate) pulse();
            start();
        };
        buttons.forEach((button, index) => {
            button.addEventListener("click", () => {
                if (performance.now() < ignoreClickUntil) return;
                select(index);
            });
        });
        dock.addEventListener("pointerdown", (event) => {
            if (event.pointerType === "mouse" && event.button !== 0) return;
            pointerActive = true;
            dragging = false;
            pointerId = event.pointerId;
            startClientX = event.clientX;
        });
        dock.addEventListener("pointermove", (event) => {
            if (!pointerActive || event.pointerId !== pointerId) return;
            const dx = event.clientX - startClientX;
            if (!dragging) {
                if (Math.abs(dx) < dragDistancePx) return;
                dragging = true;
                capturePointer(dock, event.pointerId);
                clearTimeout(releaseTimer);
                returningFlat = false;
                scale.target = settings.dock.activeScale;
                start();
            }
            const rect = dock.getBoundingClientRect();
            const raw = clamp(
                event.clientX - rect.left - pad() - cell() / 2,
                0, cell() * (buttons.length - 1)
            );
            const velocity = raw - x.target;
            x.target = raw;
            stretch.target = 1 + Math.min(settings.dock.stretch, Math.abs(velocity) / 80);
            selected = Math.round(raw / cell());
        });
        const release = (event) => {
            if (!pointerActive || event.pointerId !== pointerId) return;
            const wasDragging = dragging;
            pointerActive = false;
            dragging = false;
            pointerId = null;
            releasePointer(dock, event.pointerId);
            if (!wasDragging) return;
            ignoreClickUntil = performance.now() + 220;
            beginIdleReturn();
            select(selected, false);
            /* Releasing a drag has to land on the page, not just park
               the pill there. ancserTPX.js switches workspaces from
               each tab's own onclick, so synthesise the click the drag
               replaced. ignoreClickUntil makes our own click listener
               no-op, leaving the app's handler as the only responder. */
            buttons[selected]?.click();
        };
        const cancel = (event) => {
            if (!pointerActive || event.pointerId !== pointerId) return;
            pointerActive = false;
            dragging = false;
            pointerId = null;
            releasePointer(dock, event.pointerId);
            selected = Math.max(0, buttons.findIndex(
                (button) => button.classList.contains("active")
            ));
            x.target = targetFor(selected);
            beginIdleReturn();
            start();
        };
        dock.addEventListener("pointerup", release);
        dock.addEventListener("pointercancel", cancel);
        new ResizeObserver(() => {
            x.value = x.target = targetFor(selected);
            apply();
            syncOpticalSurfaces("dock");
        }).observe(dock);
        x.value = x.target = targetFor(selected);
        apply();
    }

    /* ── ideal #2: segment control ───────────────────────────────── */

    function initSegmentControl(track, onSelect) {
        const indicator = live(".segment-indicator", track);
        const buttons = Array.from(track.children)
            .filter((node) => node.matches("[data-segment-index]"));
        if (!indicator || !buttons.length) return;
        const contentLayers = buttons.map(
            (button) => button.querySelector(".control-source-content")
        );
        const x = new Spring(0);
        const scale = new Spring(settings.segment.idleScale);
        const stretch = new Spring(1);
        const activity = new Spring(0);
        let selected = buttons.findIndex((b) => b.classList.contains("active"));
        if (selected < 0) selected = 0;
        let maskActive = false;
        let returningFlat = true;
        const pad = () => padOf(track);
        const cell = () => (track.clientWidth - pad() * 2) / buttons.length;
        const apply = () => {
            const positionMoving = !x.settled(0.002);
            const remaining = Math.abs(x.target - x.value);
            if (positionMoving && !returningFlat && remaining <= cell() * 0.24) {
                returningFlat = true;
            }
            const glassShapeActive = positionMoving && !returningFlat;
            if (glassShapeActive) {
                activity.target = 1;
                maskActive = true;
            } else if (activity.target !== 0) {
                fastReturn(activity, 0, 0.14);
            }
            if (activity.target === 0 && activity.value <= 0.002) {
                activity.value = 0;
                activity.velocity = 0;
                maskActive = false;
            }
            const glassOpacity = clamp(activity.value, 0, 1);
            track.classList.toggle("interacting", glassShapeActive);
            track.style.setProperty("--control-glass", glassOpacity.toFixed(4));
            indicator.style.left = `${pad() + x.value}px`;
            indicator.style.width = `${cell()}px`;
            indicator.style.transform =
                `scale(${scale.value * stretch.value}, ${scale.value / stretch.value})`;
            if (!CRISP_LABELS) cutOriginalContentUnderLens(contentLayers, indicator, maskActive);
        };
        const start = () => runSpringLoop(
            "segment", [x, scale, stretch, activity], apply,
            () => [x, scale, stretch, activity].some((s) => !s.settled())
        );
        const select = (index) => {
            selected = clamp(index, 0, buttons.length - 1);
            returningFlat = false;
            activity.value = Math.max(activity.value, 0.18);
            activity.target = 1;
            maskActive = true;
            scale.target = settings.segment.activeScale;
            fastReturn(scale, settings.segment.idleScale);
            x.target = selected * cell();
            buttons.forEach((button, i) => button.classList.toggle("active", i === selected));
            if (onSelect) onSelect(buttons[selected], selected);
            start();
        };
        buttons.forEach((button, index) => {
            button.addEventListener("click", () => select(index));
        });
        new ResizeObserver(() => {
            x.value = x.target = selected * cell();
            apply();
            syncOpticalSurfaces("segment");
        }).observe(track);
        x.value = x.target = selected * cell();
        apply();
    }

    /* ── ideal #4: layered FAB ───────────────────────────────────── */

    function initLayeredFab(world) {
        const main = live(".fab-button", world);
        if (!main) return;
        const mainContent = live(".surface-content", main);
        const actions = Array.from(world.children)
            .filter((node) => node.matches("[data-fab-action]"));
        const progress = new Spring(0);
        const press = new Spring(settings.fab.idleScale);
        let open = false;
        /* Demo used a flat 70px step. Keep the same relationship to
           the button box so it tracks the rem-scaled FAB. */
        const step = () => main.offsetHeight * 1.15;
        const apply = () => {
            main.style.transform = `scale(${press.value})`;
            mainContent.style.transform = `rotate(${progress.value * 90}deg)`;
            actions.forEach((action, index) => {
                const distance = step() * (index + 1);
                const closedBottom = (main.offsetHeight - action.offsetHeight) / 2;
                const delay = index * 0.08;
                const p = clamp(
                    (progress.value - delay) / Math.max(0.01, 1 - delay), 0, 1
                );
                action.style.opacity = String(p);
                action.style.bottom = `${closedBottom + distance * p}px`;
                action.style.transform = `scale(${0.55 + p * 0.45})`;
                action.classList.toggle("open", p > 0.8);
            });
        };
        const start = () => runSpringLoop("fab", [progress, press], apply);
        main.addEventListener("pointerdown", () => {
            press.target = settings.fab.activeScale;
            start();
        });
        main.addEventListener("pointerup", () => {
            fastReturn(press, settings.fab.idleScale);
            start();
        });
        main.addEventListener("click", () => {
            open = !open;
            main.setAttribute("aria-expanded", String(open));
            progress.target = open ? 1 : 0;
            progress.velocity += (open ? 1 : -1) * settings.fab.stretch * 3;
            start();
        });
        /* Actions latch instead of navigating — these are the old
           ⌖ auto-center / ≫ jump-to-latest toggles. */
        actions.forEach((action) => {
            action.addEventListener("click", () => {
                if (action.dataset.fabLatch === "1") {
                    action.classList.toggle("latched");
                }
            });
        });
        apply();
    }

    /* ── chart pointer lens ──────────────────────────────────────────
       The gallery's Precision Lens, re-tasked as a magnifier that
       tracks the cursor across the chart.

       Position goes through left/top, NOT transform. syncOpticalSurfaces
       derives the refracted world's offset from offsetLeft/offsetTop and
       assumes any transform is pure scale — translating the lens would
       slide the element without moving its offsets, and the image inside
       would drift off the thing it is supposed to be magnifying.
       transform stays reserved for the scale/squash springs. */
    function initChartLens(lens) {
        const stage = lens.closest("[data-stage]") || lens.parentElement;
        const interactiveSelector = [
            "a", "button", "input", "select", "textarea", "summary",
            "[onclick]", "[contenteditable='true']",
            "[role='button']", "[role='switch']", "[role='link']",
            "[role='menuitem']", "[tabindex]:not([tabindex='-1'])",
            ".glass-slider", ".glass-dock", ".glass-segment", ".glass-fab",
            ".tf-chk", ".help-dot", "#bottom-drag-handle",
        ].join(",");
        const scale = new Spring(0);
        const stretchX = new Spring(1);
        const stretchY = new Spring(1);
        let px = 0;
        let py = 0;
        let lastX = 0;
        let lastY = 0;
        let lastTime = 0;
        let active = false;
        let pointerInside = false;

        const blocksLens = (target) => {
            if (!(target instanceof Element)) return false;
            if (target.closest(interactiveSelector)) return true;
            let node = target;
            while (node && node !== stage) {
                const cursor = getComputedStyle(node).cursor;
                if (["pointer", "grab", "grabbing", "move", "ew-resize",
                    "ns-resize", "col-resize", "row-resize"].includes(cursor)) {
                    return true;
                }
                node = node.parentElement;
            }
            return false;
        };

        const apply = () => {
            lens.style.left = `${px - lens.offsetWidth / 2}px`;
            lens.style.top = `${py - lens.offsetHeight / 2}px`;
            lens.style.opacity = clamp(
                scale.value / Math.max(0.01, settings.precision.idleScale), 0, 1
            ).toFixed(3);
            lens.style.transform =
                `scale(${scale.value * stretchX.value}, ${scale.value * stretchY.value})`;
        };
        const start = () => runSpringLoop(
            "precision", [scale, stretchX, stretchY], apply,
            () => active || [scale, stretchX, stretchY].some((s) => !s.settled()),
            lens
        );

        const hide = (immediate = false) => {
            active = false;
            lastTime = 0;
            if (immediate) {
                scale.value = scale.target = 0;
                scale.velocity = 0;
                stretchX.value = stretchX.target = 1;
                stretchX.velocity = 0;
                stretchY.value = stretchY.target = 1;
                stretchY.velocity = 0;
                apply();
                return;
            }
            scale.target = 0;
            fastReturn(stretchX, 1);
            fastReturn(stretchY, 1);
            start();
        };

        stage.addEventListener("pointermove", (event) => {
            if (event.pointerType === "touch") return;
            pointerInside = true;
            const rect = stage.getBoundingClientRect();
            const now = performance.now();
            const dt = Math.max(1, now - lastTime) / 1000;
            const vx = lastTime ? (event.clientX - lastX) / dt : 0;
            const vy = lastTime ? (event.clientY - lastY) / dt : 0;
            lastX = event.clientX;
            lastY = event.clientY;
            if (blocksLens(event.target)) {
                hide(true);
                return;
            }
            px = event.clientX - rect.left + stage.scrollLeft;
            py = event.clientY - rect.top + stage.scrollTop;
            lastTime = now;
            // Squash along the direction of travel, like the demo does.
            const amount = Math.min(
                settings.precision.stretch, Math.hypot(vx, vy) / 6000
            );
            const horizontal = Math.abs(vx) >= Math.abs(vy);
            stretchX.target = 1 + (horizontal ? amount : -amount * 0.5);
            stretchY.target = 1 + (horizontal ? -amount * 0.5 : amount);
            if (!active) {
                active = true;
                scale.target = settings.precision.idleScale;
            }
            apply();
            syncOpticalSurfaces("precision");
            start();
        });

        const followScroll = () => {
            if (!pointerInside) return;
            const rect = stage.getBoundingClientRect();
            const inside = lastX >= rect.left && lastX <= rect.right
                && lastY >= rect.top && lastY <= rect.bottom
                && rect.width > 0 && rect.height > 0;
            if (!inside) {
                hide(true);
                return;
            }
            const target = document.elementFromPoint(lastX, lastY);
            if (blocksLens(target)) {
                hide(true);
                return;
            }
            px = lastX - rect.left + stage.scrollLeft;
            py = lastY - rect.top + stage.scrollTop;
            if (!active) {
                active = true;
                scale.target = settings.precision.idleScale;
                start();
            }
            apply();
            syncOpticalSurfaces("precision");
        };
        stage.addEventListener("scroll", followScroll, { passive: true });
        window.addEventListener("scroll", followScroll, { passive: true });
        document.addEventListener("scroll", followScroll, { passive: true, capture: true });
        stage.addEventListener("pointerleave", () => {
            pointerInside = false;
            hide();
        });
        stage.addEventListener("pointercancel", () => {
            pointerInside = false;
            hide(true);
        });
        new MutationObserver(() => {
            if (!stage.offsetWidth || !stage.offsetHeight) hide(true);
        }).observe(stage, {
            attributes: true,
            attributeFilter: ["class", "style"],
        });
        apply();
    }

    /* ── ideal #5: fluid slider ──────────────────────────────────── */

    function initFluidSlider(root) {
        const thumb = live(".slider-thumb", root);
        const fill = live(".slider-fill", root);
        if (!thumb || !fill) return;
        /* data-output wins: it is an id, and sanitizeClone strips ids
           from clones, so it cannot resolve to a copy. The scoped
           fallback still has to be clone-guarded — the thumb's own
           optical layer holds a clone of the entire sidebar, complete
           with a .slider-value for every row. */
        const output = (root.dataset.output && byId(root.dataset.output))
            || live(".slider-value", root.closest(".param-row") || root.parentElement);
        const min = parseFloat(root.dataset.min ?? "0");
        const max = parseFloat(root.dataset.max ?? "1");
        const step = parseFloat(root.dataset.step ?? "0");
        const decimals = parseInt(root.dataset.decimals ?? "2", 10);
        const suffix = root.dataset.suffix || "";
        const scale = new Spring(settings.slider.idleScale);
        const stretchX = new Spring(1);
        const stretchY = new Spring(1);
        const activity = new Spring(0);
        let dragging = false;
        let pointerId = null;
        let value = clamp(parseFloat(root.dataset.value ?? "0.42"), 0, 1);
        let thumbX = 0;

        /* The clone must show the same fill width as the live track,
           or the refracted image lags a frame behind the thumb. */
        const surface = surfaceFor(thumb);
        if (surface) {
            surface.syncSample = (copy) => {
                const sample = copy.querySelector(
                    `[data-slider-key="${root.dataset.sliderKey}"] .slider-fill`
                );
                if (sample) sample.style.width = fill.style.width;
            };
        }

        const snap = (raw) => {
            if (!step) return raw;
            const span = max - min;
            if (span <= 0) return raw;
            const stepped = Math.round((raw * span) / step) * step;
            return clamp(stepped / span, 0, 1);
        };
        const emit = () => {
            if (!output) return;
            output.textContent = (min + value * (max - min)).toFixed(decimals) + suffix;
        };
        const travel = () => root.clientWidth - thumb.offsetWidth;
        let lastPointerX = 0;
        let lastTime = 0;
        const apply = () => {
            const center = thumbX + thumb.offsetWidth / 2;
            const edgeEpsilon = 0.5;
            const atStart = thumbX <= edgeEpsilon;
            const atEnd = thumbX >= travel() - edgeEpsilon;
            const fillWidth = atStart ? 0 : atEnd ? root.clientWidth : center;
            root.classList.toggle("endpoint-transition", atStart || atEnd);
            activity.target = dragging ? 1 : 0;
            root.classList.toggle("interacting", dragging);
            root.style.setProperty(
                "--slider-glass", clamp(activity.value, 0, 1).toFixed(4)
            );
            fill.style.width = `${fillWidth}px`;
            thumb.style.left = `${thumbX}px`;
            thumb.style.transform =
                `scale(${scale.value * stretchX.value}, ${scale.value * stretchY.value})`;
        };
        const start = () => runSpringLoop(
            "slider", [scale, stretchX, stretchY, activity], apply,
            () => dragging || [scale, stretchX, stretchY, activity]
                .some((spring) => !spring.settled())
        );
        const update = (clientX) => {
            const rect = root.getBoundingClientRect();
            const now = performance.now();
            const dt = Math.max(1, now - lastTime) / 1000;
            const velocity = lastTime ? (clientX - lastPointerX) / dt : 0;
            value = snap(clamp(
                (clientX - rect.left - thumb.offsetWidth / 2) / Math.max(1, travel()),
                0, 1
            ));
            thumbX = value * travel();
            const amount = Math.min(settings.slider.stretch, Math.abs(velocity) / 5000);
            stretchX.target = 1 + amount;
            stretchY.target = 1 - amount * 0.45;
            lastPointerX = clientX;
            lastTime = now;
            emit();
            /* Skin mode: the real <input type=range> stays in the DOM
               (hidden) and keeps its inline oninput binding, so drive
               it rather than reimplementing what it triggers. */
            const proxy = root.dataset.sliderProxy && byId(root.dataset.sliderProxy);
            if (proxy) {
                proxy.value = String(min + value * (max - min));
                proxy.dispatchEvent(new Event("input", { bubbles: true }));
            }
            apply();
            syncOpticalSurfaces("slider");
        };
        root.addEventListener("pointerdown", (event) => {
            if (event.pointerType === "mouse" && event.button !== 0) return;
            event.preventDefault();
            if (pointerId !== null && pointerId !== event.pointerId) return;
            dragging = true;
            pointerId = event.pointerId;
            lastPointerX = event.clientX;
            lastTime = performance.now();
            capturePointer(root, event.pointerId);
            activity.value = Math.max(activity.value, 0.18);
            activity.target = 1;
            scale.target = settings.slider.activeScale;
            update(event.clientX);
            start();
        });
        root.addEventListener("pointermove", (event) => {
            if (!dragging || event.pointerId !== pointerId) return;
            event.preventDefault();
            update(event.clientX);
        });
        const release = (event) => {
            if (pointerId === null || event.pointerId !== pointerId) return;
            const endingPointerId = pointerId;
            dragging = false;
            pointerId = null;
            if (event.type !== "lostpointercapture") {
                releasePointer(root, endingPointerId);
            }
            fastReturn(scale, settings.slider.idleScale);
            fastReturn(stretchX, 1);
            fastReturn(stretchY, 1);
            fastReturn(activity, 0, 0.14);
            start();
        };
        root.addEventListener("pointerup", release);
        root.addEventListener("pointercancel", release);
        root.addEventListener("lostpointercapture", release);
        root.addEventListener("dragstart", (event) => event.preventDefault());
        new ResizeObserver(() => {
            thumbX = value * travel();
            apply();
            syncOpticalSurfaces("slider");
            // Controls in the hidden Live panel boot at 0×0. Rebuild their
            // displacement maps when the tab makes them measurable.
            scheduleFilterRebuild();
        }).observe(root);
        thumbX = value * travel();
        emit();
        apply();
    }

    /* ── ideal #3: tactile switch ────────────────────────────────── */

    function initTactileSwitch(track, onChange) {
        const thumb = live(".switch-thumb", track);
        if (!thumb) return;
        const x = new Spring(0);
        const scale = new Spring(settings.switch.idleScale);
        const squash = new Spring(1);
        const activity = new Spring(0);
        let pointerActive = false;
        let dragging = false;
        let pointerId = null;
        let startClientX = 0;
        let startProgress = 0;
        let pressTime = 0;
        let committed = track.classList.contains("on") ? 1 : 0;
        /* Derive the thumb inset from the track/thumb height gap, NOT
           from getComputedStyle(thumb).left — apply() writes that same
           property every frame, so reading it back makes the inset
           feed on its own output and the thumb walks off the track.
           The design insets the thumb equally on all four sides, so
           the vertical gap is the horizontal inset, and it follows the
           rem scale for free. */
        const inset = () =>
            Math.max(0, (track.clientHeight - thumb.offsetHeight) / 2);
        const travel = () =>
            Math.max(1, track.clientWidth - thumb.offsetWidth - inset() * 2);
        const progress = () => clamp(x.value / Math.max(1, travel()), 0, 1);

        const surface = surfaceFor(thumb);
        if (surface) {
            surface.syncSample = (copy) => {
                const sample = copy.querySelector(
                    `[data-switch-key="${track.dataset.switchKey}"]`
                );
                if (!sample) return;
                sample.classList.toggle("on", track.classList.contains("on"));
            };
        }

        const beginSwitchIdleReturn = () => {
            fastReturn(scale, settings.switch.idleScale);
            fastReturn(squash, 1);
        };
        const apply = () => {
            const current = progress();
            const positionMoving = !x.settled(0.002);
            const glassShapeActive = pointerActive || positionMoving;
            if (glassShapeActive) {
                activity.target = 1;
            } else if (activity.target !== 0) {
                fastReturn(activity, 0, 0.14);
            }
            track.classList.toggle("interacting", glassShapeActive);
            track.classList.toggle("on", current >= 0.5);
            track.style.setProperty("--switch-progress", current.toFixed(4));
            track.style.setProperty(
                "--switch-glass", clamp(activity.value, 0, 1).toFixed(4)
            );
            thumb.style.left = `${inset() + x.value}px`;
            thumb.style.transform =
                `scale(${scale.value * squash.value}, ${scale.value / squash.value})`;
        };
        const start = () => runSpringLoop(
            "switch", [x, scale, squash, activity], apply,
            () => pointerActive || [x, scale, squash, activity]
                .some((spring) => !spring.settled())
        );
        const commit = (next, silent = false) => {
            const changed = committed !== (next >= 0.5 ? 1 : 0);
            committed = next >= 0.5 ? 1 : 0;
            track.setAttribute("aria-checked", String(Boolean(committed)));
            x.target = committed * travel();
            activity.value = Math.max(activity.value, 0.55);
            activity.target = 1;
            if (changed && !silent && onChange) onChange(Boolean(committed), track);
            start();
        };

        /* Lets callers move the switch for real instead of only
           re-colouring the track: the thumb position lives in a spring,
           so flipping the `on` class alone leaves the two disagreeing.
           Silent by default — the caller is the one driving state, so
           re-entering onChange would just bounce back. */
        track.tpxSetState = (on, silent = true) => commit(on ? 1 : 0, silent);
        track.addEventListener("pointerdown", (event) => {
            if (event.pointerType === "mouse" && event.button !== 0) return;
            pointerActive = true;
            dragging = false;
            pointerId = event.pointerId;
            startClientX = event.clientX;
            startProgress = progress();
            pressTime = performance.now();
            capturePointer(track, event.pointerId);
            activity.value = Math.max(activity.value, 0.18);
            activity.target = 1;
            scale.target = settings.switch.activeScale;
            squash.target = 1 + settings.switch.stretch;
            start();
        });
        track.addEventListener("pointermove", (event) => {
            if (!pointerActive || event.pointerId !== pointerId) return;
            const dx = event.clientX - startClientX;
            if (!dragging && Math.abs(dx) <= 6) return;
            dragging = true;
            const next = clamp(startProgress + dx / Math.max(1, travel()), 0, 1);
            const previous = x.value;
            x.value = x.target = next * travel();
            x.velocity = (x.value - previous) * 18;
            squash.target = 1 + Math.min(
                settings.switch.stretch,
                Math.abs(x.velocity) / Math.max(1, travel() * 24)
            );
            apply();
            syncOpticalSurfaces("switch");
        });
        const release = (event) => {
            if (!pointerActive || event.pointerId !== pointerId) return;
            const cancelled = event.type === "pointercancel";
            const elapsed = performance.now() - pressTime;
            const distance = Math.abs(event.clientX - startClientX);
            const wasDragging = dragging;
            pointerActive = false;
            dragging = false;
            pointerId = null;
            releasePointer(track, event.pointerId);
            if (cancelled) commit(committed);
            else if (wasDragging) commit(progress() >= 0.5 ? 1 : 0);
            else if (elapsed <= 500 && distance <= 6) commit(committed ? 0 : 1);
            else commit(committed);
            beginSwitchIdleReturn();
            start();
        };
        track.addEventListener("pointerup", release);
        track.addEventListener("pointercancel", release);
        track.addEventListener("lostpointercapture", (event) => {
            if (pointerActive) release(event);
        });
        track.addEventListener("keydown", (event) => {
            if (event.key !== " " && event.key !== "Enter") return;
            event.preventDefault();
            commit(committed ? 0 : 1);
        });
        new ResizeObserver(() => {
            if (!pointerActive) {
                x.value = x.target = committed * travel();
                apply();
                syncOpticalSurfaces("switch");
                // Hidden-tab switches also start with 2×2 fallback maps.
                scheduleFilterRebuild();
            }
        }).observe(track);
        /* Seed aria from the markup's `on` class; commit() is the only
           other writer and it does not run until first interaction. */
        track.setAttribute("aria-checked", String(Boolean(committed)));
        x.value = x.target = committed * travel();
        apply();
    }

    function startMirrorHeartbeat() {
        if (mirror.raf || !mirror.enabled) return;
        if (!surfaces.some((s) => s.mirrorPairs && s.mirrorPairs.length)) return;
        let last = 0;
        const tick = (now) => {
            if (now - last >= mirror.intervalMs) {
                last = now;
                syncOpticalSurfaces();
            }
            mirror.raf = requestAnimationFrame(tick);
        };
        mirror.raf = requestAnimationFrame(tick);
    }

    function stopMirrorHeartbeat() {
        if (!mirror.raf) return;
        cancelAnimationFrame(mirror.raf);
        mirror.raf = 0;
    }

    /* ── TPX theme ──────────────────────────────────────────────── */

    const THEME_KEY = "ancserTPXTheme";

    function setTheme(theme, opts = {}) {
        const light = theme === "light";
        document.documentElement.dataset.theme = light ? "light" : "dark";
        const toggle = byId("theme-switch");
        /* tpxSetState is absent on the initTheme() pass — that runs
           before the switch controller is built, and the class it sets
           is exactly what seeds `committed` a moment later. */
        if (toggle?.tpxSetState) toggle.tpxSetState(light);
        else if (toggle) toggle.classList.toggle("on", light);
        const icon = byId("theme-icon");
        if (icon) icon.textContent = light ? "☀" : "☾";
        if (!opts.silent) {
            try { localStorage.setItem(THEME_KEY, light ? "light" : "dark"); } catch (e) {}
        }
        /* Palette tokens changed, so every cached displacement/material
           map is stale — the container material map bakes the border
           colour in. Rebuild rather than let light mode wear dark rims. */
        scheduleFilterRebuild();
    }

    function initTheme() {
        let stored = null;
        try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
        setTheme(stored === "light" ? "light" : "dark", { silent: true });
    }

    /* ── bootstrap ───────────────────────────────────────────────── */

    function init() {
        /* Theme first: it stamps data-theme and the switch's `.on`
           class. initTactileSwitch reads that class to seed its
           committed position, and createContainerMaterialMap bakes
           the resolved border colour into a texture — so both would
           be wrong if the palette were applied afterwards. */
        initTheme();
        buildOpticalSurfaces();
        observeLiveStageContent();

        liveAll(".glass-dock").forEach((dock) => {
            initDragDock(dock, (button) => {
                const view = button.dataset.dockView || button.dataset.tab;
                // Always rebuild: production TPX tabs use data-tab rather
                // than the prototype's data-dock-view/data-dock-panel API.
                scheduleStageCloneRebuild();
                if (!view) return;
                /* Unscoped on purpose: the clones must switch panels
                   too, or the refracted image shows the old tab. */
                document.querySelectorAll("[data-dock-panel]").forEach((panel) => {
                    panel.hidden = panel.dataset.dockPanel !== view;
                });
            });
        });

        liveAll(".glass-segment").forEach((segment) => {
            initSegmentControl(segment, (button) => {
                const view = button.dataset.segmentView;
                if (!view) return;
                document.querySelectorAll("[data-segment-panel]").forEach((panel) => {
                    panel.hidden = panel.dataset.segmentPanel !== view;
                });
            });
        });

        liveAll(".glass-fab").forEach(initLayeredFab);
        liveAll(".glass-slider").forEach(initFluidSlider);
        liveAll(".chart-lens").forEach(initChartLens);
        /* One sweep covers every switch including the theme one —
           initialising #theme-switch separately would bind two
           independent spring loops to the same element. */
        liveAll(".glass-switch").forEach((track) => {
            initTactileSwitch(track, (on) => {
                if (track.id === "theme-switch") {
                    setTheme(on ? "light" : "dark");
                    return;
                }
                const target = track.dataset.switchTarget;
                if (target) {
                    const hidden = byId(target);
                    if (hidden) hidden.value = on ? "1" : "0";
                }
                /* Skin mode: click the original .toggle-field so its
                   inline handler (toggleTrailTrigger etc.) still runs. */
                const proxy = track.dataset.switchProxy;
                if (proxy) byId(proxy)?.click();
            });
        });

        const account = live(".glass-account");
        if (account) {
            live(".account-orb", account)?.addEventListener("click", (event) => {
                event.stopPropagation();
                account.classList.toggle("open");
            });
            document.addEventListener("click", (event) => {
                if (!account.contains(event.target)) account.classList.remove("open");
            });
        }

        window.addEventListener("resize", () => {
            scheduleOpticalSync();
            scheduleFilterRebuild();
        });
        document.addEventListener("scroll", scheduleOpticalSync, true);

        startMirrorHeartbeat();
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) stopMirrorHeartbeat();
            else startMirrorHeartbeat();
        });

        window.TpxGlass = {
            settings,
            setTheme,
            sync: scheduleOpticalSync,
            refresh: () => { scheduleOpticalSync(); scheduleFilterRebuild(); },
            /* Off => canvas-backed stages refract blank space and the
               idle backdrop-filter is all you see. Use it to A/B the
               cost of option 2 against option 1. */
            setCanvasMirror(on) {
                mirror.enabled = Boolean(on);
                if (mirror.enabled) startMirrorHeartbeat();
                else stopMirrorHeartbeat();
                scheduleOpticalSync();
            },
            setMirrorScale(scale) {
                mirror.scale = clamp(Number(scale) || 0.5, 0.1, 1);
                scheduleOpticalSync();
            },
            get diagnostics() {
                return {
                    surfaces: surfaces.length,
                    components: surfaces.reduce((acc, s) => {
                        acc[s.component] = (acc[s.component] || 0) + 1;
                        return acc;
                    }, {}),
                    theme: document.documentElement.dataset.theme,
                    mirror: {
                        enabled: mirror.enabled,
                        scale: mirror.scale,
                        pairs: surfaces.reduce(
                            (n, s) => n + (s.mirrorPairs ? s.mirrorPairs.length : 0), 0
                        ),
                        blits: mirror.blits,
                        lastCostMs: Number(mirror.lastCost.toFixed(3)),
                    },
                };
            },
        };
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
})();
