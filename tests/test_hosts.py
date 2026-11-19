import subprocess
from pathlib import Path

import pytest

from devbox import DevboxError, hosts
from fake_host import FAKE_KEY, FakeHost, done

MINI_UFW = """Status: active

To                         Action      From
--                         ------      ----
Anywhere on incusbr0       ALLOW       Anywhere
Anywhere (v6) on incusbr0  ALLOW       Anywhere (v6)

Anywhere                   ALLOW FWD   Anywhere on incusbr0
Anywhere on incusbr0       ALLOW FWD   Anywhere
Anywhere (v6)              ALLOW FWD   Anywhere (v6) on incusbr0
Anywhere (v6) on incusbr0  ALLOW FWD   Anywhere (v6)
"""

READY = {"os": "ubuntu-24.04", "user": "dev", "in_incus_admin": "0", "sudo": "1", "apt": "1", "docker": "1",
         "ufw_installed": "1", "incus_installed": "1", "incus_access": "sudo", "storage_pools": "1", "bridge": "1",
         "forward_policy": "-P FORWARD DROP", "ufw_status": MINI_UFW}


def facts(**changes):
    return {**READY, **changes}


def step_ids(plan):
    return [s["id"] for s in plan.steps]


def test_fingerprint_matches_openssh_format():
    assert hosts.fingerprint(FAKE_KEY).startswith("SHA256:") and "=" not in hosts.fingerprint(FAKE_KEY)
    with pytest.raises(DevboxError):
        hosts.fingerprint("garbage")


# Bootstrap plan


def test_ready_host_has_nothing_to_do():
    """Incus installed and initialised, UFW rules present: bootstrap reports nothing to do."""
    plan = hosts.make_plan("mini", facts())
    assert plan.nothing_to_do and plan.steps == [] and plan.blockers == []
    assert "nothing to do" in plan.lines()[0]


def test_ufw_rules_are_detected_in_ufw_status_output():
    assert hosts.ufw_rules_present(MINI_UFW) == {"in", "route_in", "route_out"}
    assert hosts.ufw_rules_present("Status: active\n\nTo  Action  From\n--  ------  ----\n22/tcp  ALLOW  Anywhere\n") == set()
    # IPv6-only rules do not count
    v6 = "Anywhere (v6) on incusbr0  ALLOW       Anywhere (v6)\n"
    assert hosts.ufw_rules_present(v6) == set()


def test_fresh_host_installs_and_initialises_incus():
    plan = hosts.make_plan("h", facts(incus_installed="0", incus_access=None, storage_pools="0", bridge="0",
                                      ufw_status="Status: inactive\n", forward_policy="-P FORWARD ACCEPT"))
    assert step_ids(plan) == ["install-incus", "init-incus"]
    assert plan.steps[0]["commands"] == ["sudo -n apt-get update",
                                         "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y incus"]
    assert plan.steps[1]["commands"] == ["sudo -n incus admin init --minimal"]
    assert plan.manual == [] and plan.blockers == []


def test_active_ufw_without_rules_gets_exactly_the_three_rules():
    plan = hosts.make_plan("h", facts(ufw_status="Status: active\n"))
    assert step_ids(plan) == ["ufw-incusbr0"]
    assert plan.steps[0]["commands"] == [
        "sudo -n ufw allow in on incusbr0",
        "sudo -n ufw route allow in on incusbr0",
        "sudo -n ufw route allow out on incusbr0",
    ]
    assert "Docker" in plan.steps[0]["title"]


def test_only_missing_ufw_rules_are_added():
    partial = "Status: active\n\nTo Action From\n--  --  --\nAnywhere on incusbr0       ALLOW       Anywhere\n"
    plan = hosts.make_plan("h", facts(ufw_status=partial))
    assert plan.steps[0]["commands"] == ["sudo -n ufw route allow in on incusbr0",
                                         "sudo -n ufw route allow out on incusbr0"]


def test_forward_drop_without_ufw_is_reported_not_edited():
    plan = hosts.make_plan("h", facts(ufw_status="Status: inactive\n"))
    assert plan.steps == [] and len(plan.manual) == 1
    assert "does not edit Docker's" in plan.manual[0]
    assert not any("iptables" in c for s in plan.steps for c in s["commands"])


def test_clean_host_without_docker_or_ufw_needs_no_firewall_rules():
    plan = hosts.make_plan("h", facts(ufw_installed="0", ufw_status="", forward_policy="-P FORWARD ACCEPT"))
    assert plan.nothing_to_do


def test_blockers_are_reported_clearly():
    no_sudo = hosts.make_plan("h", facts(incus_installed="0", sudo="0", incus_access=None))
    assert "no passwordless sudo" in no_sudo.blockers[0]
    no_access = hosts.make_plan("h", facts(incus_access="none"))
    assert "incus-admin" in no_access.blockers[0]
    no_apt = hosts.make_plan("h", facts(incus_installed="0", apt="0", incus_access=None))
    assert "no apt-get" in no_apt.blockers[0]
    ufw_no_sudo = hosts.make_plan("h", facts(sudo="0", incus_access="direct", ufw_status="Status: active\n"))
    assert "no passwordless sudo" in ufw_no_sudo.blockers[0]


def test_direct_incus_access_uses_no_sudo_for_init():
    plan = hosts.make_plan("h", facts(incus_access="direct", in_incus_admin="1", bridge="0"))
    assert plan.steps[0]["commands"] == ["incus admin init --minimal"]


def test_parse_facts_splits_ufw_table():
    out = "os=ubuntu-24.04\nsudo=1\n" + hosts.FACTS_MARKER + "\nStatus: active\n"
    parsed = hosts.parse_facts(out)
    assert parsed["os"] == "ubuntu-24.04" and parsed["sudo"] == "1" and parsed["ufw_status"] == "Status: active\n"


def host_for(tmp_path, runner):
    host = hosts.Host("mini", "dev@mini", FAKE_KEY, hosts.fingerprint(FAKE_KEY))
    return hosts.HostShell(host, tmp_path, runner)


def test_apply_runs_steps_in_order_and_rereads_facts(tmp_path):
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd[-1])
        return done("sudo=1\n" if "ufw status" in cmd[-1] or "os-release" in cmd[-1] else "")

    plan = hosts.make_plan("mini", facts(ufw_status="Status: active\n"))
    shell = host_for(tmp_path, runner)
    after = hosts.apply_plan(shell, plan)
    commands = [s for s in seen if "ufw allow" in s or "ufw route" in s]
    assert len(commands) == 3 and all(c.endswith("</dev/null'") or "</dev/null" in c for c in commands)
    assert isinstance(after, dict)


def test_apply_stops_at_the_first_failure_and_refuses_blocked_plans(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd[-1])
        return done(err="no", code=1)

    plan = hosts.make_plan("mini", facts(incus_installed="0", incus_access=None))
    with pytest.raises(DevboxError, match="install-incus.*failed"):
        hosts.apply_plan(host_for(tmp_path, runner), plan)
    assert len(calls) == 1
    blocked = hosts.make_plan("mini", facts(incus_installed="0", sudo="0", incus_access=None))
    with pytest.raises(DevboxError, match="cannot bootstrap"):
        hosts.apply_plan(host_for(tmp_path, runner), blocked)


# SSH to the host


def test_host_ssh_command_pins_the_key_under_an_alias(tmp_path):
    host = hosts.Host("mini", "dev@mini", FAKE_KEY, hosts.fingerprint(FAKE_KEY), Path("/k/id"))
    cmd = host.ssh_cmd(tmp_path, "true")
    assert cmd[:3] == ["ssh", "-i", "/k/id"]
    assert "StrictHostKeyChecking=yes" in cmd and "HostKeyAlias=devbox-host-mini" in cmd
    assert "HostKeyAlgorithms=ssh-ed25519" in cmd and cmd[-3:] == ["dev@mini", "--", "true"]
    known = (tmp_path / "known_hosts" / "host-mini").read_text()
    assert known == f"devbox-host-mini {FAKE_KEY}\n"
    assert "-i" not in hosts.Host("m", "a@b", FAKE_KEY, "x").ssh_cmd(tmp_path)


def test_scan_host_key_prefers_ed25519_and_resolves_aliases():
    def runner(cmd, **kw):
        if cmd[:2] == ["ssh", "-G"]:
            return done("user dev\nhostname mini-pc.example\nport 2222\n")
        assert cmd[0] == "ssh-keyscan" and "2222" in cmd and cmd[-1] == "mini-pc.example"
        return done(f"mini-pc.example ssh-rsa AAAArsa\nmini-pc.example {FAKE_KEY}\n# comment\n")

    assert hosts.scan_host_key("dev@mini", runner) == FAKE_KEY
    with pytest.raises(DevboxError, match="no SSH host key"):
        hosts.scan_host_key("dev@mini", lambda cmd, **kw: done("user a\nhostname h\n") if cmd[1] == "-G" else done(""))


def test_host_shell_reports_ssh_failures(tmp_path):
    shell = host_for(tmp_path, lambda cmd, **kw: done(err="Host key verification failed.", code=255))
    with pytest.raises(DevboxError, match="ssh to host mini .* failed: Host key verification failed"):
        shell.run("true")
