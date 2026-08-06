(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.TPXTopstepEval = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const TRADE_TZ = 'America/Chicago';
    const TRADE_DAY_START_HOUR_CT = 17;
    const TRADE_TIME_FORMATTER = new Intl.DateTimeFormat('en-US', {
        timeZone: TRADE_TZ,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        hourCycle: 'h23',
    });
    const DEFAULTS = Object.freeze({
        accountSize: 50000,
        baseTarget: 3000,
        maxLossLimit: 2000,
        iterations: 10000,
        horizonDays: 60,
        minimumDays: 2,
        seed: 1092026,
    });

    function finite(value, fallback) {
        const number = Number(value);
        return Number.isFinite(number) ? number : fallback;
    }

    function clampInteger(value, fallback, min, max) {
        const number = Math.floor(finite(value, fallback));
        return Math.max(min, Math.min(max, number));
    }

    function dateKey(year, month, day) {
        return year + '-' + String(month).padStart(2, '0') + '-' + String(day).padStart(2, '0');
    }

    function timePartsInZone(date) {
        const parts = TRADE_TIME_FORMATTER.formatToParts(date);
        const output = {};
        for (const part of parts) {
            if (part.type !== 'literal') output[part.type] = part.value;
        }
        return {
            year: parseInt(output.year, 10),
            month: parseInt(output.month, 10),
            day: parseInt(output.day, 10),
            hour: parseInt(output.hour, 10),
            minute: parseInt(output.minute, 10),
        };
    }

    /** Topstep trading day: 17:00 CT through 15:10 CT the next calendar day. */
    function tradeDayKey(value) {
        const date = value instanceof Date ? value : new Date(value);
        if (!date || !Number.isFinite(date.getTime())) return null;
        const parts = timePartsInZone(date);
        if (![parts.year, parts.month, parts.day, parts.hour, parts.minute].every(Number.isFinite)) return null;
        // CME/Topstep session is closed after 15:10 CT until the next trading
        // day opens at 17:00 CT. Reject stale/custom rows in that dead zone.
        if ((parts.hour === 15 && parts.minute > 10) || parts.hour === 16) return null;
        const shifted = new Date(Date.UTC(
            parts.year,
            parts.month - 1,
            parts.day + (parts.hour >= TRADE_DAY_START_HOUR_CT ? 1 : 0)
        ));
        return dateKey(shifted.getUTCFullYear(), shifted.getUTCMonth() + 1, shifted.getUTCDate());
    }

    function hashText(text) {
        let hash = 2166136261 >>> 0;
        for (let i = 0; i < text.length; i++) {
            hash ^= text.charCodeAt(i);
            hash = Math.imul(hash, 16777619) >>> 0;
        }
        return hash >>> 0;
    }

    function makeRng(seed) {
        let state = (Number(seed) >>> 0) || DEFAULTS.seed;
        return function () {
            state ^= state << 13;
            state ^= state >>> 17;
            state ^= state << 5;
            return (state >>> 0) / 4294967296;
        };
    }

    function percentile(values, probability) {
        if (!values.length) return null;
        const sorted = values.slice().sort(function (a, b) { return a - b; });
        const index = Math.max(0, Math.min(
            sorted.length - 1,
            Math.round(probability * (sorted.length - 1))
        ));
        return sorted[index];
    }

    function effectiveTarget(bestPositiveDay, baseTarget, consistencyLimit) {
        const best = Math.max(0, finite(bestPositiveDay, 0));
        const base = Math.max(0, finite(baseTarget, DEFAULTS.baseTarget));
        const limit = finite(consistencyLimit, 0.5);
        if (!(limit > 0 && limit <= 1)) throw new Error('consistencyLimit must be in (0, 1].');
        return Math.max(base, best / limit);
    }

    /**
     * Convert net backtest P&L to one-contract MNQ outcomes and preserve every
     * trade inside its settled Topstep day. Net costs scale linearly in the
     * backtest engine, so pnl / size is the exact one-MNQ result.
     */
    function buildActiveDays(trades) {
        const rows = Array.isArray(trades) ? trades : [];
        const grouped = new Map();
        const symbols = new Set();
        let skipped = 0;

        for (const trade of rows) {
            const pnl = Number(trade && trade.pnl);
            if (!Number.isFinite(pnl)) { skipped++; continue; }
            const rawSymbol = String((trade && trade.symbol) || '').replace(/^\//, '').toUpperCase();
            if (rawSymbol) symbols.add(rawSymbol);
            const size = Math.abs(Number(trade && trade.size));
            const actualSize = Number.isFinite(size) && size > 0 ? size : 1;
            const settledAt = trade && (trade.exit_time || trade.entry_time);
            const key = settledAt ? tradeDayKey(settledAt) : null;
            if (!key) { skipped++; continue; }
            const timestamp = new Date(settledAt).getTime();
            if (!grouped.has(key)) grouped.set(key, []);
            grouped.get(key).push({
                unitPnl: pnl / actualSize,
                actualSize: actualSize,
                timestamp: Number.isFinite(timestamp) ? timestamp : 0,
            });
        }

        const unsupported = Array.from(symbols).filter(function (symbol) { return symbol !== 'MNQ'; });
        if (unsupported.length) {
            return {
                ok: false,
                error: 'Topstep size comparison currently supports MNQ only (found ' + unsupported.join(', ') + ').',
                days: [],
                tradeCount: 0,
                skipped: skipped,
                symbols: Array.from(symbols),
            };
        }

        const days = Array.from(grouped.keys()).sort().map(function (key) {
            const dayTrades = grouped.get(key).slice().sort(function (a, b) {
                return a.timestamp - b.timestamp;
            });
            return {
                key: key,
                trades: dayTrades,
                unitPnl: dayTrades.reduce(function (sum, trade) { return sum + trade.unitPnl; }, 0),
            };
        });
        const tradeCount = days.reduce(function (sum, day) { return sum + day.trades.length; }, 0);
        const signature = days.map(function (day) {
            return day.key + ':' + day.trades.map(function (trade) {
                return trade.unitPnl.toFixed(6);
            }).join(',');
        }).join('|');

        return {
            ok: days.length > 0,
            error: days.length ? null : 'No settled MNQ trades are available.',
            days: days,
            tradeCount: tradeCount,
            skipped: skipped,
            symbols: Array.from(symbols),
            signature: signature,
            seed: hashText(signature),
        };
    }

    /**
     * Replay one evaluation path. MLL moves only after settled EOD equity,
     * never moves down, and locks at starting balance (relative equity zero).
     * Current trade data exposes realized P&L only, so intratrade MAE cannot be
     * checked here and callers must label the estimate as optimistic.
     */
    function simulatePath(days, sampledIndices, options) {
        options = options || {};
        const contracts = Math.max(1, finite(options.contracts, 1));
        const consistencyLimit = finite(options.consistencyLimit, 0.5);
        const baseTarget = Math.max(0, finite(options.baseTarget, DEFAULTS.baseTarget));
        const maxLossLimit = Math.max(0, finite(options.maxLossLimit, DEFAULTS.maxLossLimit));
        const minimumDays = clampInteger(options.minimumDays, DEFAULTS.minimumDays, 1, 10000);
        const slipPerContract = Math.max(0, finite(options.slippagePerContract, 0));
        const epsilon = 1e-8;
        let equity = 0;
        let floor = -maxLossLimit;
        let bestDay = 0;
        let target = baseTarget;

        for (let dayNumber = 0; dayNumber < sampledIndices.length; dayNumber++) {
            const day = days[sampledIndices[dayNumber]];
            if (!day) continue;
            const dayStart = equity;

            for (const trade of day.trades) {
                equity += (trade.unitPnl - slipPerContract) * contracts;
                if (equity <= floor + epsilon) {
                    return {
                        status: 'fail',
                        days: dayNumber + 1,
                        equity: equity,
                        floor: floor,
                        bestDay: bestDay,
                        target: target,
                        targetRaised: target > baseTarget + epsilon,
                    };
                }
            }

            const dayPnl = equity - dayStart;
            if (dayPnl > bestDay) bestDay = dayPnl;
            target = effectiveTarget(bestDay, baseTarget, consistencyLimit);

            if (dayNumber + 1 >= minimumDays && equity + epsilon >= target) {
                return {
                    status: 'pass',
                    days: dayNumber + 1,
                    equity: equity,
                    floor: floor,
                    bestDay: bestDay,
                    target: target,
                    targetRaised: target > baseTarget + epsilon,
                };
            }

            // Highest settled EOD balance is encoded by the monotonic floor.
            floor = Math.min(0, Math.max(floor, equity - maxLossLimit));
        }

        return {
            status: 'open',
            days: sampledIndices.length,
            equity: equity,
            floor: floor,
            bestDay: bestDay,
            target: target,
            targetRaised: target > baseTarget + epsilon,
        };
    }

    function summarize(bucket, iterations) {
        const passes = bucket.passDays.length;
        const fails = bucket.fail;
        const open = bucket.open;
        return {
            contracts: bucket.contracts,
            consistencyLimit: bucket.consistencyLimit,
            passCount: passes,
            failCount: fails,
            openCount: open,
            passRate: passes / iterations,
            failRate: fails / iterations,
            openRate: open / iterations,
            medianPassDays: percentile(bucket.passDays, 0.50),
            p90PassDays: percentile(bucket.passDays, 0.90),
            medianPassTarget: percentile(bucket.passTargets, 0.50),
            p90PassTarget: percentile(bucket.passTargets, 0.90),
            targetRaisedRate: bucket.targetRaised / iterations,
            targetRaisedAmongPassRate: passes ? bucket.passRaised / passes : 0,
        };
    }

    function runPairedMonteCarlo(trades, options) {
        options = options || {};
        const suppliedSource = options.source;
        const source = suppliedSource && suppliedSource.ok && Array.isArray(suppliedSource.days)
            ? suppliedSource
            : buildActiveDays(trades);
        if (!source.ok) return { ok: false, error: source.error, source: source, rows: [] };
        if (source.days.length < 5) {
            return { ok: false, error: 'Not enough Topstep days (need at least 5).', source: source, rows: [] };
        }

        const iterations = clampInteger(options.iterations, DEFAULTS.iterations, 100, 500000);
        const horizonDays = clampInteger(options.horizonDays, DEFAULTS.horizonDays, 2, 365);
        const minimumDays = clampInteger(options.minimumDays, DEFAULTS.minimumDays, 1, horizonDays);
        const baseTarget = Math.max(0, finite(options.baseTarget, DEFAULTS.baseTarget));
        const maxLossLimit = Math.max(0, finite(options.maxLossLimit, DEFAULTS.maxLossLimit));
        const slippagePerContract = Math.max(0, finite(options.slippagePerContract, 0));
        const seed = (Number(options.seed) >>> 0) || source.seed || DEFAULTS.seed;
        const sizes = (Array.isArray(options.sizes) ? options.sizes : [1, 2]).map(Number)
            .filter(function (size, index, all) {
                return Number.isFinite(size) && size > 0 && all.indexOf(size) === index;
            });
        const limits = (Array.isArray(options.consistencyLimits) ? options.consistencyLimits : [0.5, 0.4])
            .map(Number).filter(function (limit, index, all) {
                return limit > 0 && limit <= 1 && all.indexOf(limit) === index;
            });
        const configs = [];
        for (const contracts of sizes) {
            for (const consistencyLimit of limits) {
                configs.push({ contracts: contracts, consistencyLimit: consistencyLimit });
            }
        }

        const buckets = configs.map(function (config) {
            return {
                contracts: config.contracts,
                consistencyLimit: config.consistencyLimit,
                fail: 0,
                open: 0,
                targetRaised: 0,
                passRaised: 0,
                passDays: [],
                passTargets: [],
            };
        });
        const rng = makeRng(seed);
        const sampledIndices = new Array(horizonDays);

        for (let iteration = 0; iteration < iterations; iteration++) {
            for (let day = 0; day < horizonDays; day++) {
                sampledIndices[day] = Math.floor(rng() * source.days.length);
            }
            for (let index = 0; index < configs.length; index++) {
                const config = configs[index];
                const outcome = simulatePath(source.days, sampledIndices, {
                    contracts: config.contracts,
                    consistencyLimit: config.consistencyLimit,
                    baseTarget: baseTarget,
                    maxLossLimit: maxLossLimit,
                    minimumDays: minimumDays,
                    slippagePerContract: slippagePerContract,
                });
                const bucket = buckets[index];
                if (outcome.targetRaised) bucket.targetRaised++;
                if (outcome.status === 'pass') {
                    bucket.passDays.push(outcome.days);
                    bucket.passTargets.push(outcome.target);
                    if (outcome.targetRaised) bucket.passRaised++;
                } else if (outcome.status === 'fail') {
                    bucket.fail++;
                } else {
                    bucket.open++;
                }
            }
        }

        const chronological = source.days.map(function (_, index) { return index; });
        const observed = configs.map(function (config) {
            const outcome = simulatePath(source.days, chronological, {
                contracts: config.contracts,
                consistencyLimit: config.consistencyLimit,
                baseTarget: baseTarget,
                maxLossLimit: maxLossLimit,
                minimumDays: minimumDays,
                slippagePerContract: slippagePerContract,
            });
            outcome.contracts = config.contracts;
            outcome.consistencyLimit = config.consistencyLimit;
            return outcome;
        });
        const rows = buckets.map(function (bucket) { return summarize(bucket, iterations); });
        const officialLimit = limits.indexOf(0.5) >= 0 ? 0.5 : limits[0];
        const officialRows = rows.filter(function (row) { return row.consistencyLimit === officialLimit; });
        officialRows.sort(function (a, b) {
            if (b.passRate !== a.passRate) return b.passRate - a.passRate;
            return a.failRate - b.failRate;
        });
        const winner = officialRows[0] || null;
        const runnerUp = officialRows[1] || null;

        return {
            ok: true,
            source: source,
            settings: {
                iterations: iterations,
                horizonDays: horizonDays,
                minimumDays: minimumDays,
                baseTarget: baseTarget,
                maxLossLimit: maxLossLimit,
                slippagePerContract: slippagePerContract,
                seed: seed,
            },
            rows: rows,
            observed: observed,
            recommendation: winner ? {
                contracts: winner.contracts,
                passRate: winner.passRate,
                failRate: winner.failRate,
                consistencyLimit: officialLimit,
                passRateEdge: runnerUp ? winner.passRate - runnerUp.passRate : 0,
                failRateEdge: runnerUp ? runnerUp.failRate - winner.failRate : 0,
            } : null,
        };
    }

    return Object.freeze({
        DEFAULTS: DEFAULTS,
        tradeDayKey: tradeDayKey,
        effectiveTarget: effectiveTarget,
        buildActiveDays: buildActiveDays,
        simulatePath: simulatePath,
        runPairedMonteCarlo: runPairedMonteCarlo,
        makeRng: makeRng,
    });
}));
