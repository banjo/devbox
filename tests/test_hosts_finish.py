"""Stable previews per workspace, newer Incus (Zabbly), base image cache, network isolation, host cleanup (#43, #46)."""

import json

import pytest

from devbox import DevboxError, cli, config, db, hosts, incus, previews
from fake_host import FakeHost
from test_hosts import READY, facts, step_ids
from test_workspaces import add_host, run, world  # noqa: F401  (world is a fixture)


def write_config(world, text):
    (world.tmp / "config" / "devbox" / "config.toml").write_text(text)


def inc_for(tmp_path, version="6.0.0"):
    fake = FakeHost(access="incus")
    fake.version = version
    from devbox.hosts import Host, HostShell

    host = Host("mini", "dev@mini", "ssh-ed25519 AAAA", "SHA256:x")
    return fake, incus.Incus(HostShell(host, tmp_path, fake), "test")


# --- previews: stable hostnames per workspace ----------------------------------------------------


def test_hostname_has_project_and_preview_name_and_is_a_valid_label():
    assert previews.hostname("agent-1", "default", "example.com") == "agent-1.example.com"
    assert previews.hostname("agent-1", "web", "example.com") == "agent-1-web.example.com"
    assert previews.hostname("a", "b", "x.io") != previews.hostname("a", "c", "x.io")
    with pytest.raises(DevboxError, match="not a valid hostname label"):
        previews.hostname("agent-", "default", "example.com")
    with pytest.raises(DevboxError, match="longer than 63"):
        previews.hostname("p" * 42, "n" * 21, "example.com")


def test_two_projects_can_claim_the_same_label_and_are_detected():
    # project `a-b` (default preview) and project `a` with preview `b` would both be a-b.example.com
    owners = previews.label_owners(["a", "a-b", "c"], {"a": ["b"]})
    assert owners["a-b"] == {"a", "a-b"}
    assert previews.claimed_by_other("a", "b", owners) == "a-b"
    assert previews.claimed_by_other("a-b", "default", owners) == "a"
    assert previews.claimed_by_other("c", "default", owners) is None
    assert previews.claimed_by_other("a", "default", owners) is None


def test_env_does_not_announce_a_label_another_project_owns():
    owners = previews.label_owners(["a", "a-b"], {"a": ["b"]})
    env = previews.env("a-b", "example.com", [], owners)
    assert "DEVBOX_PREVIEW_URL" not in env and env["DEVBOX_PREVIEW_DOMAIN"] == "example.com"
    assert previews.env("a", "example.com", [], owners)["DEVBOX_PREVIEW_URL"] == "https://a.example.com"
    # an invalid label (trailing dash) never breaks `up`
    assert "DEVBOX_PREVIEW_URL" not in previews.env("bad-", "example.com", [])


def test_many_workspaces_on_one_host_get_distinct_stable_urls_in_their_environment(capsys, world):
    write_config(world, 'preview_domain = "example.com"\n')
    add_host(capsys, world)
    for name in ("agent-1", "agent-2"):
        (world.tmp / name).mkdir()
        assert run(capsys, "project", "add", name, "--path", str(world.tmp / name))[0] == 0
    urls = {}
    for name in ("agent-1", "agent-2"):
        code, out, err = run(capsys, "up", name, "--host", "mini", "--json")
        assert code == 0, err
        urls[name] = json.loads(out)["preview_env"]["DEVBOX_PREVIEW_URL"]
    assert urls == {"agent-1": "https://agent-1.example.com", "agent-2": "https://agent-2.example.com"}


def test_expose_refuses_a_hostname_label_that_belongs_to_another_project(capsys, world, monkeypatch):
    write_config(world, 'preview_domain = "example.com"\n')
    add_host(capsys, world)
    for name in ("crm-web", ):
        (world.tmp / name).mkdir()
        assert run(capsys, "project", "add", name, "--path", str(world.tmp / name))[0] == 0
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert run(capsys, "up", "crm-web", "--host", "mini")[0] == 0
    monkeypatch.setattr(cli.Remote, "wait_http", lambda *a, **k: (True, "200"))
    code, _, err = run(capsys, "expose", "crm", "5173", "--name", "web")  # crm-web.example.com is project crm-web
    assert code == 1 and "also what project 'crm-web' uses" in err


def test_quick_tunnel_expose_status_and_unexpose_on_a_workspace(capsys, world, monkeypatch):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert run(capsys, "up", "blog", "--host", "mini")[0] == 0
    started = []
    monkeypatch.setattr(cli.Remote, "start_tunnel",
                        lambda self, port, unit: started.append((self.ip, port, unit)) or f"https://{unit}-{port}.trycloudflare.com")
    monkeypatch.setattr(cli.Remote, "stop_unit", lambda self, unit: started.append(("stop", self.ip, unit)))
    # the same port in two workspaces: each tunnel runs inside its own container
    code, out, err = run(capsys, "expose", "crm", "5173", "--json")
    assert code == 0, err
    first = json.loads(out)
    assert first["kind"] == "quick" and first["preview_env"]["DEVBOX_PREVIEW_URL"] == first["url"]
    assert run(capsys, "expose", "blog", "5173")[0] == 0
    assert len({s[0] for s in started}) == 2 and all(s[1] == 5173 for s in started)
    status = run(capsys, "status", "crm")[1]
    assert f"preview: {first['url']} -> port 5173" in status
    assert run(capsys, "unexpose", "crm")[0] == 0
    assert ("stop", started[0][0], "tunnel") in started
    assert "preview:" not in run(capsys, "status", "crm")[1]
    assert "preview:" in run(capsys, "status", "blog")[1]  # the other workspace keeps its preview


def test_preview_environment_is_written_into_the_container(capsys, world, monkeypatch):
    add_host(capsys, world)
    written = []
    monkeypatch.setattr(cli.Remote, "write_file", lambda self, path, content, mode="600": written.append((self.ip, path, content)))
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert any(path == previews.ENV_FILE for _, path, _ in written)
    assert all(ip.startswith("10.0.0.") for ip, _, _ in written)  # the container's address, reached through the jump host


# --- newer Incus from the Zabbly repository ------------------------------------------------------


def test_plan_reports_the_incus_version_and_flags_old_ones():
    old = hosts.make_plan("mini", facts(incus_version="6.0.0", ufw_status="", forward_policy=""))
    assert any("Incus 6.0.0" in n and "--zabbly" in n and "runc 1.2.3" in n for n in old.notes)
    assert step_ids(old) == []  # nothing changes unless asked for
    new = hosts.make_plan("mini", facts(incus_version="7.6", ufw_status="", forward_policy=""))
    assert any(n.startswith("Incus 7.6") for n in new.notes) and not any("runc" in n for n in new.notes)
    assert hosts.is_old_incus("6.0.5") and hosts.is_old_incus("5.21") and not hosts.is_old_incus("6.1") \
        and not hosts.is_old_incus("7.6") and not hosts.is_old_incus("")


def test_zabbly_upgrade_is_planned_only_with_the_flag_and_checks_the_key():
    f = facts(incus_version="6.0.0", codename="noble", arch="amd64", zabbly_repo="0", ufw_status="", forward_policy="",
              running_containers="2")
    plan = hosts.make_plan("mini", f, zabbly=True)
    assert step_ids(plan) == ["zabbly-repo", "upgrade-incus"]
    repo = "\n".join(plan.steps[0]["commands"])
    assert hosts.ZABBLY_KEY_FPR in repo and "https://pkgs.zabbly.com/key.asc" in repo
    assert "Suites: noble" in repo and "Architectures: amd64" in repo and "Signed-By: /etc/apt/keyrings/zabbly.asc" in repo
    assert "rm -f /etc/apt/keyrings/zabbly.asc" in repo  # a key with another fingerprint is removed again
    assert "apt-get install -y -o Dpkg::Options::=--force-confold incus" in plan.steps[1]["commands"][0]
    assert any("2 container(s) are running" in n for n in plan.notes)
    assert not any("runc" in n for n in plan.notes)


def test_zabbly_on_a_fresh_host_installs_from_zabbly_instead_of_the_archive():
    fresh = {"os": "ubuntu-24.04", "codename": "noble", "arch": "amd64", "sudo": "1", "apt": "1", "user": "dev",
             "incus_installed": "0", "ufw_status": "", "forward_policy": ""}
    plan = hosts.make_plan("h", fresh, zabbly=True)
    assert step_ids(plan) == ["zabbly-repo", "install-incus", "init-incus"]
    assert not any("archive" in s["title"] for s in plan.steps)
    plain = hosts.make_plan("h", fresh)
    assert step_ids(plain) == ["install-incus", "init-incus"] and "archive" in plain.steps[0]["title"]
    assert any("--zabbly" in n for n in plain.notes)


def test_zabbly_skips_new_enough_hosts_and_refuses_unsupported_systems():
    recent = hosts.make_plan("h", facts(incus_version="7.6", codename="noble", arch="amd64", ufw_status="", forward_policy=""),
                             zabbly=True)
    assert step_ids(recent) == [] and any("new enough" in n for n in recent.notes)
    odd = hosts.make_plan("h", facts(incus_version="6.0.0", codename="focal", arch="amd64", ufw_status="", forward_policy=""),
                          zabbly=True)
    assert any("no packages for 'focal'" in b for b in odd.blockers)
    nosudo = hosts.make_plan("h", facts(incus_version="6.0.0", codename="noble", arch="amd64", sudo="0", ufw_status="",
                                        forward_policy=""), zabbly=True)
    assert any("passwordless sudo" in b for b in nosudo.blockers)


def test_configured_zabbly_repo_only_refreshes_and_upgrades():
    plan = hosts.make_plan("h", facts(incus_version="6.0.0", codename="noble", arch="amd64", zabbly_repo="1",
                                      ufw_status="", forward_policy=""), zabbly=True)
    assert step_ids(plan) == ["apt-update", "upgrade-incus"]


def test_forward_drop_without_ufw_stays_report_only_even_with_zabbly():
    plan = hosts.make_plan("h", facts(incus_version="6.0.0", codename="noble", arch="amd64", ufw_status="",
                                      forward_policy="-P FORWARD DROP"), zabbly=True)
    assert not any("ufw" in s["id"] or "iptables" in " ".join(s["commands"]) for s in plan.steps)
    assert any("FORWARD policy is DROP" in m and "does not edit" in m for m in plan.manual)


def test_bootstrap_applies_zabbly_only_with_the_flag_and_yes(capsys, world, monkeypatch):
    add_host(capsys, world)
    old = {"incus_installed": "1", "incus_access": "sudo", "storage_pools": "1", "bridge": "1", "sudo": "1", "apt": "1",
           "user": "dev", "ufw_status": "", "forward_policy": "", "incus_version": "6.0.0", "codename": "noble",
           "arch": "amd64"}
    monkeypatch.setattr(hosts, "collect_facts", lambda shell: dict(old))
    applied = []
    monkeypatch.setattr(hosts, "apply_plan", lambda shell, plan, say=None: applied.append(plan) or {**old, "incus_version": "7.6"})
    code, out, _ = run(capsys, "host", "bootstrap", "mini", "--yes")  # --yes alone never upgrades Incus
    assert code == 0 and applied == [] and "--zabbly" in out
    code, out, _ = run(capsys, "host", "bootstrap", "mini", "--zabbly")  # the flag alone only shows the plan
    assert code == 0 and applied == [] and "upgrade Incus 6.0.0" in out and "--zabbly --yes" in out
    code, out, _ = run(capsys, "host", "bootstrap", "mini", "--zabbly", "--yes", "--json")
    assert code == 0 and len(applied) == 1 and [s["id"] for s in applied[0].steps] == ["zabbly-repo", "upgrade-incus"]
    data = json.loads(out)
    assert data["applied"] is True and data["next"]["steps"]


def test_facts_script_reads_what_the_plan_needs():
    for needle in ("incus_version=", "codename=", "arch=", "zabbly_repo=", "running_containers="):
        assert needle in hosts.FACTS_SCRIPT


def test_commands_of_a_plan_keep_their_pipes_when_stdin_is_closed(tmp_path):
    fake = FakeHost(access="incus")
    shell = hosts.HostShell(hosts.Host("h", "u@h", "ssh-ed25519 AAAA", "SHA256:x"), tmp_path, fake)
    plan = hosts.make_plan("h", facts(incus_version="6.0.0", codename="noble", arch="amd64", zabbly_repo="0",
                                      ufw_status="", forward_policy=""), zabbly=True)
    hosts.apply_plan(shell, plan)
    tee = next(s for s in fake.scripts if "| sudo -n tee" in s)
    assert tee.startswith("( ") and tee.endswith(" ) </dev/null")  # the redirect wraps the pipeline, not tee


def test_runc_workaround_is_reported_and_zabbly_suggested(capsys, world):
    add_host(capsys, world)
    world.exec_out["hello-world"] = incus.WORKAROUND_MARKER + "\n"
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0, err
    assert any("runc 1.2.3 workaround" in w and "--zabbly" in w for w in json.loads(out)["warnings"])


# --- base image cache ----------------------------------------------------------------------------


def test_base_alias_changes_with_the_setup_the_image_and_the_incus_version(monkeypatch):
    a = incus.base_alias("ubuntu/24.04", "6.0.0")
    assert a == incus.base_alias("ubuntu/24.04", "6.0.0") and a.startswith("devbox-base-")
    assert a != incus.base_alias("ubuntu/22.04", "6.0.0")
    assert a != incus.base_alias("ubuntu/24.04", "7.6")  # a cached image may carry the runc workaround
    monkeypatch.setattr(incus, "DOCKER_CHECK", incus.DOCKER_CHECK + "\n# changed")
    assert a != incus.base_alias("ubuntu/24.04", "6.0.0")
    monkeypatch.undo()
    monkeypatch.setattr(incus, "SSHD_SETUP", incus.SSHD_SETUP + " ")
    assert a != incus.base_alias("ubuntu/24.04", "6.0.0")


def test_first_up_publishes_the_base_image_and_later_workspaces_start_from_it(capsys, world):
    add_host(capsys, world)
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0, err
    assert json.loads(out)["image_cache"] == "published"
    alias = incus.base_alias("ubuntu/24.04", "6.0.0")
    assert [i["aliases"][0]["name"] for i in world.images] == [alias]
    assert world.images[0]["properties"] == {"user.devbox.base": "true", "user.devbox.controller": world.controller}
    assert world.launched_from["devbox-crm"] == "images:ubuntu/24.04"
    code, out, err = run(capsys, "up", "blog", "--host", "mini", "--json")
    assert code == 0 and json.loads(out)["image_cache"] == "hit"
    assert world.launched_from["devbox-blog"] == alias
    assert len(world.images) == 1  # not published again


def test_the_published_image_holds_no_keys_and_is_published_before_they_are_written(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    inside = [s for s in world.scripts if s.startswith("[devbox-crm]")]
    clean = next(i for i, s in enumerate(inside) if "truncate -s 0 /etc/machine-id" in s)
    keys = next(i for i, s in enumerate(inside) if "authorized_keys &&" in s)
    assert clean < keys and "ssh_host_*_key" in inside[clean]
    publish = next(i for i, c in enumerate(world.calls) if c[0] == "publish")
    stop = next(i for i, c in enumerate(world.calls) if c[:2] == ["stop", "devbox-crm"])
    start = next(i for i, c in enumerate(world.calls) if c[:2] == ["start", "devbox-crm"] and i > publish)
    assert stop < publish < start  # the container is stopped for the publish and running again afterwards


def test_a_new_incus_version_invalidates_the_cache_and_removes_the_old_image(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    old = world.images[0]["aliases"][0]["name"]
    world.version = "7.6"
    assert run(capsys, "up", "blog", "--host", "mini", "--json")[0] == 0
    assert world.launched_from["devbox-blog"] == "images:ubuntu/24.04"  # old image no longer matches
    aliases = [i["aliases"][0]["name"] for i in world.images]
    assert aliases == [incus.base_alias("ubuntu/24.04", "7.6")] and old not in aliases


def test_cache_can_be_switched_off_and_a_failed_publish_does_not_fail_up(capsys, world):
    write_config(world, "incus_image_cache = false\n")
    add_host(capsys, world)
    code, out, _ = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0 and json.loads(out)["image_cache"] == "off" and world.images == []
    assert not any(c[0] == "publish" for c in world.calls)


def test_a_failing_publish_only_costs_the_cache(capsys, world, monkeypatch):
    add_host(capsys, world)
    real = world.incus

    def failing(args):
        return real(args) if args[0] != "publish" else __import__("fake_host").done(err="disk full", code=1)

    monkeypatch.setattr(world, "incus", failing)
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0, err
    assert json.loads(out)["image_cache"] == "off" and world.containers["devbox-crm"]["status"] == "Running"
    assert "Could not publish the base image" in err


def test_only_our_base_images_are_listed_and_deleted(tmp_path):
    fake, inc = inc_for(tmp_path)
    fake.images = [
        {"fingerprint": "aa", "aliases": [{"name": "devbox-base-111"}],
         "properties": {"user.devbox.base": "true", "user.devbox.controller": "test"}},
        {"fingerprint": "bb", "aliases": [{"name": "devbox-base-222"}],
         "properties": {"user.devbox.base": "true", "user.devbox.controller": "someone-else"}},
        {"fingerprint": "cc", "aliases": [{"name": "my-own-image"}], "properties": {}},
    ]
    assert [i["alias"] for i in inc.base_images()] == ["devbox-base-111"]
    with pytest.raises(DevboxError, match="does not touch it"):
        inc.delete_base_image("bb")
    with pytest.raises(DevboxError, match="does not touch it"):
        inc.delete_base_image("cc")
    inc.delete_base_image("aa")
    assert [i["fingerprint"] for i in fake.images] == ["bb", "cc"]


def test_host_remove_deletes_our_cached_image_only(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    world.images.append({"fingerprint": "zz", "aliases": [{"name": "devbox-base-other"}],
                         "properties": {"user.devbox.base": "true", "user.devbox.controller": "other"}})
    assert run(capsys, "down", "crm")[0] == 0
    code, out, _ = run(capsys, "host", "remove", "mini", "--json")
    assert code == 0 and json.loads(out)["deleted_base_images"] == [incus.base_alias("ubuntu/24.04", "6.0.0")]
    assert [i["fingerprint"] for i in world.images] == ["zz"]


# --- network isolation between workspaces --------------------------------------------------------


def test_isolation_is_off_by_default_and_a_launch_option_when_asked(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert not incus.Incus.isolated(world.containers["devbox-crm"])
    assert not any("-d" in c for c in world.calls if c[0] == "launch")
    code, out, err = run(capsys, "up", "blog", "--host", "mini", "--isolate")
    assert code == 0, err
    launch = next(c for c in world.calls if c[0] == "launch" and "devbox-blog" in c)
    assert launch[launch.index("-d") + 1] == "eth0,security.port_isolation=true"
    assert incus.Incus.isolated(world.containers["devbox-blog"]) and ", isolated" in out


def test_config_turns_isolation_on_for_new_workspaces_and_no_isolate_overrides_it(capsys, world):
    write_config(world, "incus_isolate = true\n")
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert incus.Incus.isolated(world.containers["devbox-crm"])
    assert run(capsys, "up", "blog", "--host", "mini", "--no-isolate")[0] == 0
    assert not incus.Incus.isolated(world.containers["devbox-blog"])


def test_isolation_of_an_existing_workspace_can_be_changed(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    code, out, err = run(capsys, "up", "crm", "--isolate", "--json")
    assert code == 0, err
    assert any(c[:4] == ["config", "device", "override", "devbox-crm"] for c in world.calls)
    assert incus.Incus.isolated(world.containers["devbox-crm"])
    assert json.loads(out)["warnings"] == []  # the NIC change is live, nothing to restart
    assert "isolated" in run(capsys, "status", "crm")[1]
    run(capsys, "up", "crm", "--no-isolate")
    assert any(c[:3] == ["config", "device", "set"] for c in world.calls)  # the NIC is overridden now: set it


def test_changing_isolation_keeps_the_workspace_limits(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini", "--cpus", "3", "--memory", "6GiB")[0] == 0
    code, out, _ = run(capsys, "up", "crm", "--isolate")
    assert code == 0 and "3 cpu, 6GiB, isolated" in run(capsys, "status", "crm")[1]
    assert world.containers["devbox-crm"]["config"]["limits.memory"] == "6GiB"


def test_isolate_needs_a_workspace_and_a_valid_config_value(capsys, world):
    add_host(capsys, world)
    code, _, err = run(capsys, "up", "crm", "--isolate")
    assert code == 1 and "only apply to workspaces" in err
    write_config(world, 'incus_isolate = "yes"\n')
    code, _, err = run(capsys, "status")
    assert code == 1 and "incus_isolate must be true or false" in err


# --- cleanup and doctor on hosts -----------------------------------------------------------------


def hosts_only(world, monkeypatch):
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Hetzner must not be asked"))
    monkeypatch.setattr(world.hetzner, "list_all_servers", boom)


def test_cleanup_lists_an_unused_labelled_container_and_deletes_only_with_yes(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    world.add_container("devbox-planted", project="planted")  # ours by label, in nobody's registry
    world.add_container("devbox-theirs", controller="another-install", project="x")
    world.add_container("not-devbox", labels=False)
    code, out, _ = run(capsys, "cleanup")
    assert code == 0
    assert "workspace container devbox-planted" in out and "project planted" in out
    assert "container devbox-crm" not in out and "not-devbox" not in out and "container devbox-theirs" not in out
    assert "another devbox install (devbox-theirs)" in out
    assert "devbox-planted" in world.containers  # listing only
    code, out, _ = run(capsys, "cleanup", "--yes")
    assert code == 0 and "devbox-planted" not in world.containers
    assert {"devbox-crm", "devbox-theirs", "not-devbox"} <= set(world.containers)
    assert "devbox-planted" not in run(capsys, "cleanup")[1]


def test_cleanup_json_names_the_host_and_kind(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    world.add_container("devbox-planted", project="planted", status="Stopped")
    code, out, _ = run(capsys, "cleanup", "--json")
    item = json.loads(out)["items"][0]
    assert (item["kind"], item["host"], item["id"]) == ("workspace container", "mini", "devbox-planted")
    assert "stopped" in item["reason"]


def test_a_paused_workspace_is_not_unused(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert run(capsys, "pause", "crm")[0] == 0
    assert run(capsys, "cleanup")[1].startswith("nothing to clean up")


def test_cleanup_lists_outdated_base_images_but_keeps_the_current_one(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert run(capsys, "cleanup")[1].startswith("nothing to clean up")
    world.images.append({"fingerprint": "old1", "aliases": [{"name": "devbox-base-old"}],
                         "properties": {"user.devbox.base": "true", "user.devbox.controller": world.controller}})
    out = run(capsys, "cleanup")[1]
    assert "base image devbox-base-old" in out and "outdated" in out
    assert run(capsys, "cleanup", "--yes")[0] == 0
    assert len(world.images) == 1 and world.images[0]["aliases"][0]["name"] == incus.base_alias("ubuntu/24.04", "6.0.0")


def test_cleanup_does_not_list_the_pinned_host_keys(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    run(capsys, "status")
    assert (config.load().state_dir / "known_hosts" / "host-mini").exists()
    assert run(capsys, "cleanup")[1].startswith("nothing to clean up")


def test_cleanup_notes_an_unreachable_host_instead_of_failing(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    world.access = "none"
    code, out, _ = run(capsys, "cleanup")
    assert code == 0 and "host mini not checked for unused containers" in out


def test_doctor_warns_about_unused_containers_without_a_hetzner_token(capsys, world, monkeypatch):
    add_host(capsys, world)
    hosts_only(world, monkeypatch)
    world.hetzner.token_error = "no token"
    code, out, _ = run(capsys, "doctor", "--json")
    checks = [c for c in json.loads(out)["checks"] if c["check"] == "host leftovers"]
    assert [c["status"] for c in checks] == ["ok"]
    world.add_container("devbox-planted", project="planted")
    code, out, _ = run(capsys, "doctor", "--json")
    checks = [c for c in json.loads(out)["checks"] if c["check"] == "host leftovers"]
    assert checks[0]["status"] == "warn" and "container devbox-planted on mini" in checks[0]["detail"]
    assert checks[0]["fix"] == "see `devbox cleanup`"


def test_doctor_with_hetzner_counts_host_leftovers_separately(capsys, world):
    add_host(capsys, world)
    world.add_container("devbox-planted", project="planted")
    code, out, _ = run(capsys, "doctor", "--json")
    checks = json.loads(out)["checks"]
    assert any(c["check"] == "host leftovers" and c["status"] == "warn" for c in checks)
    assert not any(c["check"] == "leftovers" and "planted" in c["detail"] for c in checks)


# --- agent guidance ------------------------------------------------------------------------------


def test_host_add_prints_and_returns_next_steps(capsys, world):
    code, out, _ = run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--yes")
    assert code == 0 and "next: `devbox host bootstrap mini`" in out and "devbox up PROJECT --host mini" in out
    run(capsys, "host", "remove", "mini")
    code, out, _ = run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--yes", "--json")
    data = json.loads(out)
    assert data["reachable"] is True and data["next"]["steps"][0].startswith("`devbox host bootstrap mini`")


def test_host_add_next_step_when_the_login_fails(capsys, world):
    world.access = "none"  # the fake host still answers `true`; make the login itself fail
    world_call = world.__class__.__call__

    def refuse(self, cmd, **kw):
        if "true" in (cmd[-1] if cmd else ""):
            return __import__("fake_host").done(err="Permission denied", code=255)
        return world_call(self, cmd, **kw)

    world.__class__.__call__ = refuse
    try:
        code, out, _ = run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--yes", "--json")
    finally:
        world.__class__.__call__ = world_call
    data = json.loads(out)
    assert code == 0 and data["reachable"] is False and "fix SSH access" in data["next"]["steps"][0]


def test_bootstrap_json_and_text_say_what_to_do_next(capsys, world, monkeypatch):
    add_host(capsys, world)
    ready = {"incus_installed": "1", "incus_access": "sudo", "storage_pools": "1", "bridge": "1", "sudo": "1", "apt": "1",
             "user": "dev", "ufw_status": "", "forward_policy": "", "incus_version": "7.6"}
    monkeypatch.setattr(hosts, "collect_facts", lambda shell: dict(ready))
    code, out, _ = run(capsys, "host", "bootstrap", "mini")
    assert "nothing to do" in out and "next: `devbox up PROJECT --host mini`" in out
    data = json.loads(run(capsys, "host", "bootstrap", "mini", "--json")[1])
    assert data["nothing_to_do"] is True and data["next"]["steps"][0].startswith("`devbox up PROJECT --host mini`")


def test_up_on_a_host_returns_next_steps(capsys, world):
    add_host(capsys, world)
    code, out, _ = run(capsys, "up", "crm", "--host", "mini", "--json")
    nxt = json.loads(out)["next"]
    assert nxt["state"] == "new" and nxt["steps"]


# --- found in manual testing ---------------------------------------------------------------------


def test_a_project_name_ending_in_a_dash_is_refused_before_anything_is_created(capsys, world):
    add_host(capsys, world)
    (world.tmp / "p4-").mkdir()
    assert run(capsys, "project", "add", "p4-", "--path", str(world.tmp / "p4-"))[0] == 0
    code, _, err = run(capsys, "up", "p4-", "--host", "mini")
    assert code == 1 and "ends with a dash" in err
    assert not any(c[0] == "launch" for c in world.calls) and db.active_server(db.connect(config.load().db_path), "p4-") is None
    assert incus.container_name("a--b") == "devbox-a--b"
    assert incus.container_name("x" * 41) == "devbox-" + "x" * 41


def test_the_base_image_is_published_uncompressed(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    publish = next(c for c in world.calls if c[0] == "publish")
    assert publish[publish.index("--compression") + 1] == "none"


def test_publishing_is_skipped_when_the_startup_deadline_leaves_too_little_time(capsys, world, monkeypatch):
    add_host(capsys, world)
    monkeypatch.setattr(cli, "PUBLISH_MIN_SECONDS", 10 ** 6)
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0 and json.loads(out)["image_cache"] == "off" and world.images == []
    assert "Skipping the base image" in err and world.containers["devbox-crm"]["status"] == "Running"
    monkeypatch.setattr(cli, "PUBLISH_MIN_SECONDS", 0)
    assert json.loads(run(capsys, "up", "blog", "--host", "mini", "--json")[1])["image_cache"] == "published"


def test_history_shows_workspaces_as_free_on_their_host(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini", "--cpus", "3", "--memory", "6GiB")[0] == 0
    out = run(capsys, "history")[1]
    row = next(line for line in out.splitlines() if "crm" in line)
    assert "host mini" in row and "free" in row and "€?" not in row and "3 cpu, 6GiB" in row
