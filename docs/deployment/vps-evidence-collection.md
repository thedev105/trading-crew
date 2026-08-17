# VPS setup: Lighter–dYdX forward evidence collection

## Scope and boundary

This deploys **only** the read-only public-data collection this project already documents in
`README.md` (sections 4-6): hourly funding-cycle collection, hourly funding health audits, and
continuous public order-book sampling for the Lighter–dYdX research pair. Every command below
calls unauthenticated public venue endpoints.

This setup does **not** involve API keys, wallets, signers, custody, or any account, order, or
trading authority. It cannot place a trade. Its only purpose is to accumulate the 90+ continuous
days of point-in-time evidence that `polytrading carry economics` needs before it can even produce
a decision — and per `docs/superpowers/specs/2026-08-13-lighter-dydx-shadow-economics-design.md`
section 10.3, a positive `SHADOW_CANDIDATE` decision from that gate is itself only a prerequisite
for a *separate*, not-yet-written forward paper-execution design — not for live trading. Do not
add credentials or an execution surface to this host.

## 1. VPS sizing

- **CPU/RAM:** 1 vCPU / 1 GB RAM is enough — the collector is I/O-bound, not compute-bound.
- **Disk:** budget for continuous growth. Book sampling runs at 5s intervals across 4 venues × 3
  assets, 24/7. Estimate actual bytes/day empirically after the first 24h (see §6) and size the
  disk with headroom for 90+ days; rotate/compress logs (§5) so they don't compound the growth.
- **OS:** any current Debian/Ubuntu LTS. Outbound HTTPS to the venues' public REST/WS endpoints
  must be permitted; no inbound ports are required.

## 2. Provision the host

```bash
# As a non-root deploy user (adjust to your distro's package manager):
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git

sudo useradd --system --create-home --shell /usr/sbin/nologin polytrading || true
sudo -u polytrading -H bash -c '
  cd ~
  git clone <this-repo-url> poly-trading
  cd poly-trading
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -e ".[dev]"
  mkdir -p var
'
```

Replace `<this-repo-url>` with wherever you push this checkout (a private remote — this repo is
not meant to be public given the account/venue research it documents).

Verify the install:

```bash
sudo -u polytrading /home/polytrading/poly-trading/.venv/bin/polytrading --help
```

## 3. systemd units

Prefer systemd over cron here: the book collector is a long-lived process, and systemd gives you
automatic restart, structured logs via `journalctl`, and clean start-on-boot without a PID-file
guard. Funding-cycle and health stay as `.timer` units (they're one-shot, like cron).

Create `/etc/systemd/system/polytrading-books.service`:

```ini
[Unit]
Description=polytrading: continuous Lighter-dYdX public book sampling
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=polytrading
WorkingDirectory=/home/polytrading/poly-trading
ExecStart=/home/polytrading/poly-trading/.venv/bin/polytrading collect books \
  --venue all --assets BTC,ETH,SOL --interval-seconds 5 --duration-seconds 86400 \
  --db /home/polytrading/poly-trading/var/forward.duckdb
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

`--duration-seconds 86400` keeps each run bounded to a day; `Restart=always` means systemd starts
the next one immediately on exit, so evidence collection never has a gap wider than the process
restart time.

Create `/etc/systemd/system/polytrading-funding-cycle.service`:

```ini
[Unit]
Description=polytrading: hourly funding-cycle collection

[Service]
Type=oneshot
User=polytrading
WorkingDirectory=/home/polytrading/poly-trading
ExecStart=/home/polytrading/poly-trading/.venv/bin/polytrading collect funding-cycle \
  --db /home/polytrading/poly-trading/var/forward.duckdb --current --assets BTC,ETH,SOL --format json
StandardOutput=append:/home/polytrading/poly-trading/var/funding-cycle.log
StandardError=append:/home/polytrading/poly-trading/var/funding-cycle.log
```

Create `/etc/systemd/system/polytrading-funding-cycle.timer`:

```ini
[Unit]
Description=Run polytrading-funding-cycle hourly at :01

[Timer]
OnCalendar=*-*-* *:01:00
Persistent=true

[Install]
WantedBy=timers.target
```

Create `/etc/systemd/system/polytrading-funding-health.service`:

```ini
[Unit]
Description=polytrading: hourly funding health audit

[Service]
Type=oneshot
User=polytrading
WorkingDirectory=/home/polytrading/poly-trading
ExecStart=/home/polytrading/poly-trading/.venv/bin/polytrading funding health \
  --db /home/polytrading/poly-trading/var/forward.duckdb --hours 24 --format json
StandardOutput=append:/home/polytrading/poly-trading/var/funding-health.log
StandardError=append:/home/polytrading/poly-trading/var/funding-health.log
```

Create `/etc/systemd/system/polytrading-funding-health.timer`:

```ini
[Unit]
Description=Run polytrading-funding-health hourly at :06

[Timer]
OnCalendar=*-*-* *:06:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable everything:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polytrading-books.service
sudo systemctl enable --now polytrading-funding-cycle.timer
sudo systemctl enable --now polytrading-funding-health.timer
```

### Cron alternative

If you'd rather not use systemd, the equivalent cron entries (Linux ships `flock`, unlike macOS):

```cron
1 * * * * cd /home/polytrading/poly-trading && .venv/bin/polytrading collect funding-cycle --db var/forward.duckdb --current --assets BTC,ETH,SOL --format json >> var/funding-cycle.log 2>&1
6 * * * * cd /home/polytrading/poly-trading && .venv/bin/polytrading funding health --db var/forward.duckdb --hours 24 --format json >> var/funding-health.log 2>&1
2 0 * * * cd /home/polytrading/poly-trading && flock -n var/books-collector.lock .venv/bin/polytrading collect books --venue all --assets BTC,ETH,SOL --interval-seconds 5 --duration-seconds 86400 --db var/forward.duckdb >> var/books-collector.log 2>&1
```

Install with `sudo -u polytrading crontab -e`.

## 4. Verify it's running

```bash
# systemd path:
sudo systemctl status polytrading-books.service
sudo journalctl -u polytrading-books.service -f

# either path, after the first couple hours:
sudo -u polytrading /home/polytrading/poly-trading/.venv/bin/polytrading funding health \
  --db /home/polytrading/poly-trading/var/forward.duckdb --hours 24 --format text
```

Exit code `0` from `funding health` means every audited hourly boundary so far has at least one
complete attempt; `degraded`/`critical` (exit `1`) is expected and fine for the first day, since
Bybit-style instrument-specification warmup and any transient venue hiccups are normal — the
command distinguishes recorded-but-degraded evidence from missing evidence.

## 5. Log rotation

Point-in-time evidence lives in `var/forward.duckdb`, not the logs — the `.log` files are
operational tails only, safe to rotate. Add `/etc/logrotate.d/polytrading`:

```
/home/polytrading/poly-trading/var/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
}
```

## 6. Disk monitoring

```bash
du -sh /home/polytrading/poly-trading/var/forward.duckdb
```

Check this after the first 24h to get an actual bytes/day figure for this host's venue set, then
extrapolate to the 90+ day window and confirm the disk has headroom. If growth is a problem, the
sampling interval can be widened with `--interval-seconds` on the books collector — but note this
trades away evidence density that the economics gate's depth/basis/latency reserves are specified
against (see the design doc §9.5), so treat it as a last resort, not a default.

## 7. Backing up `var/forward.duckdb`

This file is the entire point of running this host — back it up off-box periodically (e.g. a
nightly `rsync`/`scp` to storage you control) so a VPS failure doesn't erase the accumulated
evidence window and force a restart of the 90-day clock.

## 8. What comes after evidence accumulates

Once `var/forward.duckdb` has 90+ continuous days, run the actual research gate (from wherever you
review results, using a copy of the database — not on the VPS):

```bash
.venv/bin/polytrading carry economics --asset BTC --db var/forward.duckdb --policy <policy.json> --as-of <cutoff> --format text
```

`INSUFFICIENT_EVIDENCE` or `REJECTED` are valid, expected research outcomes — see the design doc
for what each means. Only a `SHADOW_CANDIDATE` decision opens the door to designing the next gate
(forward paper execution), and even that still requires a new spec, frozen parameters, its own
90-day forward window, and a distinct explicit approval before anything resembling execution
exists. Nothing in this deployment authorizes skipping that.
