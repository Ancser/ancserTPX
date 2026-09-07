(function (root) {
    'use strict';
    root.ANCSER_SYSTEM = {
        marketClockVersion: 'america-new-york-v1',
        timeZones: {
            data: 'UTC',
            market: 'America/New_York',
            topstep: 'America/Chicago',
            pi_source: 'America/Los_Angeles',
        },
        frontMonthContracts: { MNQ: 'MNQ', ENQ: 'ENQ', MES: 'MES' },
        contractSpecs: {},
    };
}(typeof globalThis !== 'undefined' ? globalThis : this));
