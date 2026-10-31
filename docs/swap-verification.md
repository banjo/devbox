# Default swap verification

Validation ran remotely on the existing Ubuntu 24.04 CPX22 devbox; no additional server was
created and no builds/tests were run on the controller host.

## Automated checks

- `uv run --frozen pytest -q`: **284 passed**.
- Config coverage: default 2048 MiB, zero/custom size, invalid negative/bool/float/string/list.
- CLI tests inspect the actual user-data passed to server creation for zero/default/custom size.
- Helper tests cover creation, permissions, allocation failure, low disk, command failure,
  activation readback, repeat calls, foreign active swap, inactive file reuse, fstab repair,
  duplicate/conflicting entries and unsafe file types/ownership/link counts.
- Sensitivity check: restoring the old cloud-init template makes all three rendering regression
  cases fail; restoring the changed template returns the complete suite to green.
- `uv build --wheel`: passed; archive inspection confirms `cloud-init.yaml`, `bootstrap.py`
  and `setup_swap.py` are packaged.
- `cloud-init schema --config-file <rendered.yaml>`: **Valid schema**.

## Live helper checks

The server already had a 2 GiB `/swapfile`. An unmodified `configure_swap(2048)` reused it
without changing the active swap table or `/etc/fstab`.

To exercise allocation without disabling that existing swap, an isolated fixture used:

- A disposable 64 MiB swap file and temporary fstab under `/srv/qa`.
- The real `mkswap`, `swapon`, `swapoff`, `blkid` and file allocation operations.
- Inventory output filtered to the fixture's path, allowing the no-active-swap branch to run
  while retaining the host's swap. This filtering is test-only, not production behavior.

Observed output:

```text
LIVE existing-swap reuse: Reusing active swap; no new swap file created
LIVE forced-allocation fixture: Swap active and persistent: .../swapfile (64 MiB)
LIVE repeated call: Reusing active swap; no new swap file created
LIVE snapshot-style inactive reuse: Swap active and persistent: .../swapfile (64 MiB)
LIVE fstab activation verified (no host reboot)
LIVE cleanup verified: original host swap remains active, /etc/fstab unchanged
```

The test deactivated only its own file between checks, reactivated the existing signature
without formatting, then verified `swapon --all --fstab <isolated-fstab>`. It removed its own
swap and temporary files afterward. The original host swap remained active throughout.

## Limits

No new VM provisioning, actual reboot or Hetzner snapshot round-trip was performed for this
change. Boot wiring is covered by cloud-init schema/rendering and CLI tests; inactive-file
reactivation and fstab activation were exercised with real Linux swap operations. A full
fresh-provision/reboot/snapshot acceptance run remains a separate infrastructure check.
