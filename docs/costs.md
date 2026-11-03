# Costs

devbox records what every server and snapshot costs, can warn or stop at a monthly budget, and tries
to get the most out of each billing hour.

## Tracking and budget

- `up` stores the server's hourly and monthly price from Hetzner when it creates it, and every
  server's and snapshot's start and end time stays in the registry. Servers are estimated the way
  Hetzner bills them: per started hour, capped at the monthly price per calendar month. Snapshots
  are estimated per GB per month, prorated by the hour. Each server's primary IPv4 address is
  billed on top, per started hour and capped at its own monthly price; `up` stores that price for
  the server's location from Hetzner's pricing API (`ipv4_price_monthly` in the config is the
  fallback). Every cost (`status`, `down`, `history`, `costs`, the budget) includes it, and
  `costs --json` shows it as `ipv4_cost`/`ipv4_total`. Prices include VAT; traffic is not included.
- Reports compute each month's cost once and spread it evenly over the time the server ran that
  month. A period or a day/month row gets its share, so rows always add up to the total and adjacent
  periods add up to the combined period (billed hours in a partial period can be fractional).
  Rounded rows (6 decimals in JSON, cents in the table, 2 decimals for hours) are adjusted with the
  largest-remainder method so they still add up exactly to the rounded total. `costs` rejects
  impossible or empty dates, `--days` outside 1–36500, and periods where `--until` is not after the
  start.
- `status` shows the cost so far and this month's total, `down`/`pause` print the server's total and
  activity (syncs, execs, background starts, previews), and `devbox costs` sums any period by
  project, month, day, server type or server. Removing a project keeps its history.
- `monthly_budget = 20` in the config makes `up` warn at 80% and when the month's spending reaches
  the budget; with `budget_action = "block"` it refuses to create a server unless `--over-budget` is
  passed. `status` shows what is left.

## Using the paid hour

Hetzner bills every started hour from when a server is created until it is deleted (stopped servers
are billed too). A server deleted after 5 minutes costs a full hour; six 5-minute jobs on six new
servers cost six hours. Once an hour has started it is paid, so devbox can keep an idle server until
just before the next hour starts and reuse it for the next job.

- `idle_timeout = "hour-end"` (or `devbox project set NAME --idle-timeout hour-end`): a server that
  has been idle for at least `min_idle` (default `5m`, so it isn't stopped between two jobs) is
  paused or deleted in a window just before its next billing hour. If the window passes (work was
  running, or no check ran in time), the new hour is already paid, so it waits for the window before
  the following hour.
- `devbox pause NAME --at-hour-end` / `devbox down NAME --at-hour-end`: for workflows that stop the
  server after each job. The server stays usable and is paused or deleted in the window before its
  next billing hour. Using it again (`up`, `exec`, `sync`, `touch`, `ssh`, `expose`, `forward`,
  `db restore`) cancels the stop; `ps`, `logs`, `pull` and `db dump` don't. `--dump` can't be
  combined with it.
- The window is one check interval long and ends `billing_margin` (default `5m`) plus the expected
  action time before the boundary, so exactly one scheduled check falls inside it. The action time
  is the longest of the last five pauses (snapshot and delete) or deletes devbox timed on this
  machine (`action-times.json` in the state folder), 5 minutes for pause and 1 minute for down until
  it has measured one. With the 5-minute autostop entry a pause starts 15 minutes before the
  boundary at the earliest; with a hand-made schedule devbox assumes 15 minutes between checks and
  starts earlier.
- Hours count from the creation time Hetzner reports, which `status`, `up` and `idle-check` copy
  into the registry.
- A running foreground `exec` or `--keep-alive` job postpones the stop to the next hour, as for
  ordinary timeouts. `up` waits for a stop that is in progress, then restores from the new snapshot.
- `status` shows `paid until HH:MM, pause at about HH:MM`; `status --json` and `idle-check --json`
  have `policy` (`timeout`, `hour-end`, `requested` or `off`), `paid_until`, `planned_at`,
  `planned_action` and `missed_boundary`. `autostop status` also says when a billing hour passed
  without the stop.
- Both need a working schedule (`devbox autostop enable`); `--at-hour-end` warns when nothing would
  carry it out.
- `devbox costs` shows how often a paid hour was reused instead of starting a new server, and an
  estimate of what that saved (one billed hour at the server's price each; restore time not counted;
  `reuse` in `--json`). A reuse is using a server again (`up`, `exec`, `sync`, `touch`, ...) that
  cancels a `--at-hour-end` stop, or that comes after an `hour-end` server was idle for at least
  `min_idle`. Looking at it (`ps`, `logs`) doesn't count.

## Other ways to spend less

- When a project's servers this month are on track to cost at least as much as one server kept
  running all month (each new server pays its own started hours, and its own monthly cap), `status`
  says so and suggests `project set NAME --idle-timeout off` (`monthly_cap_hint` in
  `status --json`).
- Run parallel jobs on one server (`exec --detach`) instead of starting a second one; each server
  pays its own hours.
- Use `down` instead of `pause` when nothing on the server needs to survive: no snapshot storage and
  no restore time.
- `server-types` lists the cheapest first. Since June 2026 the `cpx` and `ccx` families cost 2–3
  times more than `cx` for the same cores and memory.
