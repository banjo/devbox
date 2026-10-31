import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from devbox import DevboxError, config, setup_swap as swap
from devbox.bootstrap import render_cloud_init


def test_default_and_configured_swap(tmp_path):
    path = tmp_path / "config.toml"
    assert config.load(path).swap_size_mib == 2048
    for size in (0, 512, 4096):
        path.write_text(f"swap_size_mib = {size}\n")
        assert config.load(path).swap_size_mib == size


@pytest.mark.parametrize("value", ['-1', 'true', '2.5', '"2048"', '[]'])
def test_reject_invalid_swap_config(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f"swap_size_mib = {value}\n")
    with pytest.raises(DevboxError, match="swap_size_mib"):
        config.load(path)


@pytest.mark.parametrize("size", [0, 512, 2048])
def test_rendered_cloud_init_contains_complete_helper(size):
    text = render_cloud_init(size)
    assert "__DEVBOX_SWAP_" not in text
    assert f'python3 /usr/local/sbin/devbox-setup-swap.py {size} || exit $?' in text
    assert 'permissions: "0700"' in text
    source = Path(swap.__file__).read_text().rstrip()
    assert "\n".join("      " + line if line else "" for line in source.splitlines()) in text
    assert text.index("- python3 /usr/local/sbin/devbox-setup-swap.py") < text.index("- mkdir -p /srv/app")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    path, fstab = tmp_path / "swapfile", tmp_path / "fstab"
    fstab.write_text("# original\n/dev/root / ext4 defaults 0 1\n")
    state = {"active": [], "calls": [], "type": "swap", "fail": None}

    def command(*args):
        state["calls"].append(args)
        if state["fail"] == args[0]:
            raise subprocess.CalledProcessError(1, args)
        if args[0] == "swapon" and "--show=NAME" in args:
            return "\n".join(state["active"])
        if args[0] == "swapon":
            state["active"].append(args[1])
        if args[0] == "blkid":
            return state["type"]
        return ""

    monkeypatch.setattr(swap, "command", command)
    monkeypatch.setattr(swap.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * swap.DISK_RESERVE))
    # Run non-root on CI too; ownership validation has separate tests below.
    monkeypatch.setattr(swap, "validate_file", lambda p: None)
    return path, fstab, state


def test_create_verify_and_repeat_without_reformatting(machine):
    path, fstab, state = machine
    original = fstab.read_text()
    assert "active and persistent" in swap.configure_swap(1, path, fstab)
    assert path.stat().st_size == swap.MIB
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert fstab.read_text().startswith(original)
    assert ("mkswap", str(path)) in state["calls"]
    assert ("swapon", str(path)) in state["calls"]
    assert "Reusing" in swap.configure_swap(4096, path, fstab)
    assert path.stat().st_size == swap.MIB  # never resize existing swap
    assert state["calls"].count(("mkswap", str(path))) == 1
    assert state["calls"].count(("swapon", str(path))) == 1
    assert fstab.read_text().count(f"{path} none swap sw 0 0") == 1


def test_reuse_existing_swap_without_touching_files(machine):
    path, fstab, state = machine
    state["active"] = ["/swapfile", "/dev/zram0"]
    before = fstab.read_bytes()
    assert "Reusing" in swap.configure_swap(2048, path, fstab)
    assert not path.exists()
    assert fstab.read_bytes() == before
    assert len(state["calls"]) == 1


def test_disabled_does_not_even_probe_or_disable_existing_swap(machine):
    path, fstab, state = machine
    state["active"] = [str(path)]
    assert "disabled" in swap.configure_swap(0, path, fstab)
    assert state["calls"] == []


def test_restore_inactive_swap_reuses_without_mkswap(machine):
    path, fstab, state = machine
    path.write_bytes(b"existing swap contents")
    before = path.read_bytes()
    swap.configure_swap(2048, path, fstab)
    assert path.read_bytes() == before
    assert ("blkid", "-p", "-s", "TYPE", "-o", "value", str(path)) in state["calls"]
    assert not any(call[0] == "mkswap" for call in state["calls"])


def test_do_not_overwrite_non_swap_file(machine):
    path, fstab, state = machine
    path.write_text("valuable")
    state["type"] = "ext4"
    before = fstab.read_bytes()
    with pytest.raises(RuntimeError, match="non-swap"):
        swap.configure_swap(2048, path, fstab)
    assert path.read_text() == "valuable"
    assert fstab.read_bytes() == before


def test_low_disk_refuses_before_allocating(machine, monkeypatch):
    path, fstab, state = machine
    monkeypatch.setattr(swap.shutil, "disk_usage", lambda _: SimpleNamespace(free=swap.DISK_RESERVE))
    with pytest.raises(RuntimeError, match="disk space"):
        swap.configure_swap(2048, path, fstab)
    assert not path.exists()
    assert not any(call[0] == "mkswap" for call in state["calls"])


def test_allocation_failure_cleans_partial_file(machine, monkeypatch):
    path, fstab, state = machine
    def fail(*args):
        raise OSError("allocation failure")
    monkeypatch.setattr(swap.os, "posix_fallocate", fail)
    with pytest.raises(OSError, match="allocation failure"):
        swap.configure_swap(1, path, fstab)
    assert not path.exists()


@pytest.mark.parametrize("failed_command", ["mkswap", "swapon"])
def test_command_failures_are_visible(machine, failed_command):
    path, fstab, state = machine
    before = fstab.read_bytes()
    state["fail"] = failed_command
    with pytest.raises(subprocess.CalledProcessError):
        swap.configure_swap(1, path, fstab)
    assert fstab.read_bytes() == before


def test_persist_repair_for_already_active_managed_file(machine):
    path, fstab, state = machine
    path.write_bytes(b"swap")
    state["active"] = [str(path)]
    swap.configure_swap(1, path, fstab)
    assert f"{path} none swap sw 0 0" in fstab.read_text()
    assert len(state["calls"]) == 1


def test_fstab_without_trailing_newline_and_conflict(tmp_path):
    path, fstab = tmp_path / "swapfile", tmp_path / "fstab"
    fstab.write_text("# original")
    swap.persist(path, fstab)
    assert fstab.read_text() == f"# original\n{path} none swap sw 0 0\n"
    swap.persist(path, fstab)
    assert fstab.read_text().count(str(path)) == 1
    fstab.write_text(f"{path} /data ext4 defaults 0 0\n")
    before = fstab.read_bytes()
    with pytest.raises(RuntimeError, match="conflicting"):
        swap.persist(path, fstab)
    assert fstab.read_bytes() == before


@pytest.mark.parametrize("mode,uid,links", [
    (stat.S_IFLNK, 0, 1), (stat.S_IFDIR, 0, 1), (stat.S_IFREG, 1000, 1), (stat.S_IFREG, 0, 2),
])
def test_unsafe_existing_file_refused(mode, uid, links):
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links))
    with pytest.raises(RuntimeError, match="unsafe swap file"):
        swap.validate_file(path)


def test_safe_existing_file_accepted():
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFREG, st_uid=0, st_nlink=1))
    swap.validate_file(path)


def test_activation_is_read_back(machine, monkeypatch):
    path, fstab, state = machine
    original = swap.command
    def missing(*args):
        result = original(*args)
        return "" if args[0] == "swapon" and "--show=NAME" in args else result
    monkeypatch.setattr(swap, "command", missing)
    with pytest.raises(RuntimeError, match="could not be verified"):
        swap.configure_swap(1, path, fstab)


def test_mkswap_failure_can_be_retried(machine):
    path, fstab, state = machine
    state["fail"] = "mkswap"
    with pytest.raises(subprocess.CalledProcessError):
        swap.configure_swap(1, path, fstab)
    assert not path.exists()
    state["fail"] = None
    assert "active and persistent" in swap.configure_swap(1, path, fstab)


@pytest.mark.parametrize("options", ["noauto", "sw,noauto", "defaults,noauto,nofail"])
def test_noauto_fstab_entry_is_not_claimed_persistent(tmp_path, options):
    path, fstab = tmp_path / "swapfile", tmp_path / "fstab"
    content = f"{path} none swap {options} 0 0\n"
    fstab.write_text(content)
    with pytest.raises(RuntimeError, match="conflicting"):
        swap.persist(path, fstab)
    assert fstab.read_text() == content


def test_failing_swap_helper_stops_bootstrap(tmp_path):
    text = render_cloud_init(2048)
    line = next(line[4:] for line in text.splitlines()
                if line.startswith("  - python3 /usr/local/sbin/devbox-setup-swap.py"))
    helper = tmp_path / "failed-helper.py"
    helper.write_text("raise SystemExit(37)\n")
    marker = tmp_path / "later-command"
    line = line.replace("/usr/local/sbin/devbox-setup-swap.py", str(helper))
    # Model cloud-init's non-errexit /bin/sh: a later success must not mask the failure.
    result = subprocess.run(["sh", "-c", f"{line}\nprintf continued > {marker}"], check=False)
    assert result.returncode == 37
    assert not marker.exists()
