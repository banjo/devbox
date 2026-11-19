"""`devbox host ...` and `up/pause/down/status` for workspaces, against a fake own host."""

import json

import pytest

from devbox import cli, db, config, hosts, providers
from devbox.providers import Capabilities
from devbox.providers.ssh import SshProvider
from fake_host import FAKE_KEY, FakeHost
from fake_provider import FakeProvider


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "config" / "devbox").mkdir(parents=True)
    (tmp_path / "crm").mkdir()
    (tmp_path / "blog").mkdir()
    fake = FakeHost(controller=config.load().get_controller_id())
    hetzner = FakeProvider()
    monkeypatch.setitem(providers._REGISTRY, "hetzner", hetzner)

    class TestSsh(SshProvider):
        runner = fake

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.runner = fake

    monkeypatch.setitem(providers._REGISTRY, "ssh", TestSsh)
    monkeypatch.setattr(hosts, "scan_host_key", lambda target, runner=None: FAKE_KEY)
    monkeypatch.setattr(cli, "generate_host_key", lambda: ("PRIVATE", "ssh-ed25519 HOSTPUB"))
    monkeypatch.setattr(cli, "ensure_local_ssh_key", lambda key: "ssh-ed25519 AAAA devbox")
    calls = []
    monkeypatch.setattr(cli.Remote, "wait_ssh", lambda self, deadline, progress: calls.append(("wait_ssh", self.ip, self.jump)))
    monkeypatch.setattr(cli.Remote, "verify_tools", lambda self, deadline, progress: None)
    monkeypatch.setattr(cli.Remote, "sync", lambda self, path, manifest, timeout=None:
                        {"mode": "git", "files": 1, "deleted": 0, "warning": None})
    monkeypatch.setattr(cli.Remote, "write_file", lambda self, path, content, mode="600": None)
    monkeypatch.setattr(cli.Remote, "list_units", lambda self: [])
    monkeypatch.setattr(cli.Remote, "overview", lambda self: ([], None))
    monkeypatch.setattr(cli.Remote, "running_work", lambda self: [])
    monkeypatch.setattr(cli.admin, "_run", lambda cmd: None)
    fake.hetzner, fake.remote_calls = hetzner, calls
    fake.tmp = tmp_path
    return fake


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def add_host(capsys, world):
    code, out, err = run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--yes")
    assert code == 0, err
    for name in ("crm", "blog"):
        assert run(capsys, "project", "add", name, "--path", str(world.tmp / name))[0] == 0


# host add / list / remove


def test_host_add_pins_the_key_and_needs_confirmation(capsys, world):
    code, out, err = run(capsys, "host", "add", "mini", "--ssh", "dev@mini")
    assert code == 1 and hosts.fingerprint(FAKE_KEY) in err and "--fingerprint" in err
    assert run(capsys, "host", "list")[1].startswith("no hosts")
    code, out, err = run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--fingerprint", "SHA256:wrong")
    assert code == 1 and "nothing was pinned" in err
    code, out, err = run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--fingerprint", hosts.fingerprint(FAKE_KEY))
    assert code == 0 and "pinned ssh-ed25519" in out
    code, out, _ = run(capsys, "host", "list", "--json")
    assert json.loads(out)[0]["name"] == "mini" and json.loads(out)[0]["fingerprint"] == hosts.fingerprint(FAKE_KEY)
    assert run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--yes")[0] == 1


def test_host_names_and_targets_are_validated(capsys, world):
    assert run(capsys, "host", "add", "Bad Name", "--ssh", "dev@mini", "--yes")[0] == 1
    assert run(capsys, "host", "add", "ok", "--ssh", "no-user", "--yes")[0] == 1
    assert run(capsys, "host", "add", "ok", "--ssh", "a@b; rm -rf /", "--yes")[0] == 1


def test_host_remove_refuses_with_workspaces_and_force_deletes_only_ours(capsys, world):
    add_host(capsys, world)
    world.add_container("devbox-other", labels=False)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    code, _, err = run(capsys, "host", "remove", "mini")
    assert code == 1 and "still has workspaces (crm)" in err and "devbox-crm" in world.containers
    code, out, _ = run(capsys, "host", "remove", "mini", "--force")
    assert code == 0 and "deleted workspaces crm" in out and "machine was not touched" in out
    assert "devbox-crm" not in world.containers and "devbox-other" in world.containers
    assert run(capsys, "host", "list")[1].startswith("no hosts")


def test_host_bootstrap_prints_a_plan_and_applies_only_with_yes(capsys, world, monkeypatch):
    add_host(capsys, world)
    ready = {"incus_installed": "0", "sudo": "1", "apt": "1", "user": "dev", "ufw_status": "", "forward_policy": ""}
    monkeypatch.setattr(hosts, "collect_facts", lambda shell: dict(ready))
    applied = []
    monkeypatch.setattr(hosts, "apply_plan", lambda shell, plan, say=None: applied.append(plan) or
                        {**ready, "incus_installed": "1", "incus_access": "sudo", "storage_pools": "1", "bridge": "1"})
    code, out, _ = run(capsys, "host", "bootstrap", "mini")
    assert code == 0 and "install Incus" in out and "--yes" in out and applied == []
    code, out, err = run(capsys, "host", "bootstrap", "mini", "--yes")
    assert code == 0 and len(applied) == 1 and "nothing to do" in out


# up / exec path / pause / down


def test_up_creates_a_labelled_container_and_records_provider_and_host(capsys, world):
    add_host(capsys, world)
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--cpus", "3", "--memory", "6GiB")
    assert code == 0, err
    c = world.containers["devbox-crm"]
    assert c["config"]["limits.cpu"] == "3" and c["config"]["user.devbox.managed"] == "true"
    assert "created workspace devbox-crm on host mini" in out
    conn = db.connect(config.load().db_path)
    server = db.active_server(conn, "crm")
    assert (server["provider"], server["host"], server["provider_id"]) == ("ssh", "mini", "devbox-crm")
    assert server["status"] == "running" and server["ip"] == c["state"]["network"]["eth0"]["addresses"][0]["address"]
    assert server["host_key"] == "ssh-ed25519 HOSTPUB"
    # every later connection goes through the pinned jump host
    assert world.remote_calls[-1][2].target == "dev@mini"
    # provisioning ran inside the container
    assert any(s.startswith("[devbox-crm]") and "get.docker.com" in s for s in world.scripts)
    assert world.hetzner.calls == []  # Hetzner was not involved at all


def test_two_workspaces_on_one_host_do_not_count_against_max_servers(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    code, _, err = run(capsys, "up", "blog", "--host", "mini")
    assert code == 0, err
    assert {"devbox-crm", "devbox-blog"} <= set(world.containers)
    code, out, _ = run(capsys, "status", "--json")
    assert [p["server"]["host"] for p in json.loads(out)["projects"]] == ["mini", "mini"]


def test_pause_stops_the_container_and_up_starts_it_again(capsys, world):
    add_host(capsys, world)
    run(capsys, "up", "crm", "--host", "mini")
    code, out, _ = run(capsys, "pause", "crm")
    assert code == 0 and "container stopped, files and volumes kept" in out
    assert world.containers["devbox-crm"]["status"] == "Stopped"
    assert not any(c[0] == "delete" for c in world.calls)
    code, out, _ = run(capsys, "status", "crm")
    assert "paused workspace devbox-crm on host mini" in out and "cost: none (own hardware)" in out
    code, _, err = run(capsys, "exec", "crm", "--", "true")
    assert code == 1 and "paused" in err
    code, out, _ = run(capsys, "up", "crm")  # no --host: the workspace is already on one
    assert code == 0 and "started workspace" in out
    assert world.containers["devbox-crm"]["status"] == "Running"
    conn = db.connect(config.load().db_path)
    assert db.active_server(conn, "crm")["power"] == "running"


def test_down_deletes_the_container_and_forgets_the_server(capsys, world):
    add_host(capsys, world)
    run(capsys, "up", "crm", "--host", "mini")
    code, out, _ = run(capsys, "down", "crm")
    assert code == 0 and "deleted workspace devbox-crm" in out and "devbox-crm" not in world.containers
    conn = db.connect(config.load().db_path)
    assert db.active_server(conn, "crm") is None
    code, out, _ = run(capsys, "down", "crm")
    assert "has no server" in out


def test_up_refuses_a_foreign_container_with_the_same_name(capsys, world):
    add_host(capsys, world)
    world.add_container("devbox-crm", labels=False)
    code, _, err = run(capsys, "up", "crm", "--host", "mini")
    assert code == 1 and "not created by this devbox" in err
    assert world.mutating() == []


def test_up_moves_nothing_between_hosts_and_hetzner_servers_conflict(capsys, world):
    add_host(capsys, world)
    run(capsys, "up", "crm", "--host", "mini")
    assert run(capsys, "host", "add", "two", "--ssh", "dev@two", "--yes")[0] == 0
    code, _, err = run(capsys, "up", "crm", "--host", "two")
    assert code == 1 and "already runs on host mini" in err


def test_up_with_unknown_host_and_flags_without_host(capsys, world):
    run(capsys, "project", "add", "crm", "--path", str(world.tmp / "crm"))
    code, _, err = run(capsys, "up", "crm", "--host", "nowhere")
    assert code == 1 and "unknown host 'nowhere'" in err
    code, _, err = run(capsys, "up", "crm", "--cpus", "2")
    assert code == 1 and "only apply to workspaces" in err


def test_failed_setup_keeps_the_container_and_up_retries_it(capsys, world):
    add_host(capsys, world)
    world.exec_fail["get.docker.com"] = 1
    code, _, err = run(capsys, "up", "crm", "--host", "mini")
    assert code == 1 and "Startup failed during cloud-init" in err and "Retry startup: devbox up crm" in err
    assert "devbox-crm" in world.containers
    conn = db.connect(config.load().db_path)
    assert db.active_server(conn, "crm")["setup_error"]
    world.exec_fail.clear()
    code, out, _ = run(capsys, "up", "crm")
    assert code == 0 and db.active_server(conn, "crm")["status"] == "running"
    assert len([c for c in world.calls if c[0] == "launch"]) == 1


def test_changing_limits_on_an_existing_workspace(capsys, world):
    add_host(capsys, world)
    run(capsys, "up", "crm", "--host", "mini")
    assert run(capsys, "up", "crm", "--cpus", "1", "--memory", "1GiB")[0] == 0
    assert world.containers["devbox-crm"]["config"]["limits.cpu"] == "1"
    code, out, _ = run(capsys, "status", "crm")
    assert "1 cpu, 1GiB" in out


# capabilities


def test_ssh_provider_capabilities():
    caps = SshProvider.capabilities
    assert (caps.can_create, caps.can_delete, caps.has_snapshots, caps.has_billing, caps.has_offers) == \
        (False, False, False, False, False)
    assert caps.workspaces and not Capabilities().workspaces


def test_workspaces_have_no_cost_no_hour_end_and_no_idle_shutdown(capsys, world):
    add_host(capsys, world)
    run(capsys, "up", "crm", "--host", "mini")
    code, _, err = run(capsys, "pause", "crm", "--at-hour-end")
    assert code == 1 and "billing" in err
    code, out, _ = run(capsys, "costs", "--json")
    assert json.loads(out)["total"] == 0 and json.loads(out)["servers"] == 0
    world.hetzner.calls.clear()
    before = len(world.calls)
    code, out, _ = run(capsys, "idle-check", "--dry-run")
    assert code == 0 and "idle shutdown off" in out
    assert "devbox-crm" in world.containers and not any(c[0] in ("stop", "delete") for c in world.calls[before:])


def test_ssh_provider_refuses_machine_operations():
    from devbox import DevboxError

    provider = SshProvider("test", None, lambda: None)
    for call in (lambda: provider.delete_server(1), lambda: provider.poweron(1), lambda: provider.shutdown(1),
                 lambda: provider.create_snapshot(1, "p", "d"), lambda: provider.create_server()):
        with pytest.raises(DevboxError):
            call()
    assert provider.list_servers() == [] and provider.list_snapshots() == []


def test_status_works_without_a_hetzner_token_when_only_hosts_are_used(capsys, world, monkeypatch):
    add_host(capsys, world)
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    world.hetzner.fail_list = True
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Hetzner must not be asked"))
    monkeypatch.setattr(world.hetzner, "list_all_servers", boom)
    run(capsys, "up", "crm", "--host", "mini")
    assert run(capsys, "status")[0] == 0
