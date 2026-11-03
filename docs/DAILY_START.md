# Daily terminal start

## Operation

The App and Web share the **LIVE → ACCOUNT MAIN → Daily start · Terminal**
control. The shipped default is **OFF**. Select a fixed main account and its
PI preset first. Check Daily start, choose a time (default **06:30 Pacific**),
and SAVE to install the current-user Windows task and enable the setting.
The setting displays the default preset and contract count before activation.

Windows must be signed in and its time zone must be **Pacific Standard Time**
(Pacific Time, with automatic daylight saving). The task requests wake from
sleep; powered-off machines and logged-out sessions cannot launch an interactive
terminal. Windows power policy may prevent wake. The task invokes a small
windowless check at the scheduled time and at logon. That check opens a visible
terminal only when due. It does not launch Web or the desktop App.

The check uses `America/Los_Angeles`, skips weekends and the existing options
holiday/early-close exclusion calendar, accepts at most ten minutes of lateness,
and claims at most one attempt at a time. O3 replaces an existing terminal each
scheduled trading day: it first pauses new entries and waits for an idle-tick
acknowledgement, checks broker positions and open orders, stops the old process,
checks the broker again, and then launches the configured preset. A position,
open order, unconfirmed status, broker failure, or insufficient time prevents
the switch and reports the reason. STOP disables future automatic starts.
The pause request expires if the supervisor disappears; an interrupted stop with
unknown ownership needs operator reconciliation and a fresh explicit enable.
O1 records claimed, starting, engine-starting, healthy,
failed, runtime-failed and unknown states. It retries up to three times inside
the original ten-minute window after a child has exited before engine startup.
Once engine startup begins, an uncertain outcome is reported for operator
inspection and does not trigger a duplicate launch. The supervisor waits up to
three minutes for a healthy engine; an unresponsive process is not killed.
No prior day's missed signal is replayed into trading.

Windows process creation time is checked against the recorded terminal heartbeat.
A PID reused by a newer process no longer blocks startup. A stale heartbeat whose
process identity cannot be disproved continues to block a duplicate launch.

The scheduled module calls the existing `run_terminal_live()` path directly.
Account selection, preset normalization, contract rollover, warmup, engine,
per-account ownership lease, PI filtering, risk management and broker protections
stay in their existing implementations. The account ID and preset name resolved
when enabled must still match at launch; a mismatch stops startup. Changes to a
preset's contents retain the existing terminal semantics and take effect at its
next start. No preset is edited by Daily start.

## Stopping

App/Web **STOP** and terminal Ctrl+C persist **Daily start OFF** and cancel a
pending scheduled launch. App/Web STOP also asks the terminal to stop and waits
for acknowledgement. A timeout reports failure with the schedule already OFF;
the UI must continue treating runtime status as uncertain. Only an explicit
Daily start enable + SAVE restores scheduled launching.

Unchecking Daily start and saving disables future starts; use STOP to also stop
the current terminal. Stop retains the existing engine's position/order cleanup
semantics. It is not an additional flatten operation.

The normal `windows terminal.bat` still starts immediately. Scheduled launches
invoke the Python module directly, so they avoid the batch launcher's legacy
process termination and cache-reset operations. Previously running terminals
need to be restarted by the user to load the new control code.

O2: while the terminal cannot confirm control status, new automated entries
are blocked and the existing engine continues managing its positions; monitoring
retries and honours a confirmed STOP. A previously submitted pending order can
still fill at the broker. Confirm broker positions and protective orders when
the control status is stale. Any STOP request that cannot reach the control
database has an unknown outcome until the terminal or broker state is checked.

## State and scope

- State: `ancserMarketData/runtime/state/daily_start.sqlite3`.
- Scheduled terminal log: `ancserMarketData/runtime/logs/terminal_daily.log`.
- Windows task: `ancserTPX Daily Terminal` (current user, limited privileges).
- App/Web controls retain localhost/session/CSRF protection.
- Terminal state appears in `/api/live/status` and `/api/live/status-all`.
- These controls currently schedule **TopstepX PI futures**, using the configured
  default account. Robinhood remains its existing separate ALERT-only account.
- The terminal maintains live PI polling while running; completed startup
  catch-up remains record-only. The existing pre-07:00 Pacific PI replay filter
  remains in force: a 06:30 start prepares the engine, and accepted PI events
  begin under the current signal rules.

## Delivery verification

The O1/O2 change has isolated tests documented in `VERIFICATION.md`. The
currently running terminal retains the code and preset it loaded when it started;
the updated code takes effect after a controlled restart. Windows wake, real
broker execution and a production scheduled launch of this patch remain to be
verified.
