import shlex
from pathlib import Path

import pytest

from devbox import DevboxError, hosts, incus
from devbox.remote import Jump, Remote
from fake_host import FAKE_KEY, FakeHost


def make(tmp_path, access="sudo", controller="test"):
    fake = FakeHost(access, controller)
    host = hosts.Host("mini", "dev@mini", FAKE_KEY, hosts.fingerprint(FAKE_KEY))
    return fake, incus.Incus(hosts.HostShell(host, tmp_path, fake), controller)


# cloud-init reuse


def test_cloud_init_file_is_parsed_for_containers():
    files, commands = incus.cloud_init_steps()
    by_path = {f["path"]: f for f in files}
    assert "/etc/profile.d/devbox.sh" in by_path
    assert 'MISE_TRUSTED_CONFIG_PATHS=/srv/app' in by_path["/etc/profile.d/devbox.sh"]["content"]
    assert by_path["/etc/profile.d/devbox.sh"]["mode"] == "0644"
    joined = "\n".join(commands)
    for needle in ("mkdir -p /srv/app", "get.docker.com", "mise.run", "cloudflared", "build-essential"):
        assert needle in joined


def test_unknown_cloud_init_shapes_fail_loudly():
    with pytest.raises(DevboxError, match="unsupported"):
        incus.parse_cloud_init("write_files:\n  - path: /x\n    owner: root\n")
    with pytest.raises(DevboxError, match="found no"):
        incus.parse_cloud_init("#cloud-config\n")


# ownership


def test_only_labelled_containers_of_this_controller_are_ours(tmp_path):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-a", project="a")
    fake.add_container("devbox-b", controller="someone-else")
    fake.add_container("devbox-plain", labels=False)
    fake.add_container("web", labels=False)
    assert [c["name"] for c in inc.list_workspaces()] == ["devbox-a"]
    assert [c["name"] for c in inc.foreign_workspaces()] == ["devbox-b"]
    assert inc.get("devbox-a")["name"] == "devbox-a"
    assert inc.get("devbox-missing") is None


@pytest.mark.parametrize("name", ["devbox-plain", "devbox-b", "web"])
def test_foreign_containers_are_never_touched(tmp_path, name):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-plain", labels=False)
    fake.add_container("devbox-b", controller="someone-else")
    fake.add_container("web", labels=False)
    for action in (inc.start, inc.stop, inc.delete, inc.require, lambda n: inc.exec(n, "true"),
                   lambda n: inc.set_limits(n, 1, "1GiB"), lambda n: inc.provision(n, "k", "p", "q")):
        with pytest.raises(DevboxError, match="not created by this devbox"):
            action(name)
    assert fake.mutating() == []
    assert name in fake.containers


def test_create_refuses_an_existing_foreign_container(tmp_path):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-x", labels=False)
    with pytest.raises(DevboxError, match="not created by this devbox"):
        inc.create("devbox-x", "x", "ubuntu/24.04", 2, "4GiB")
    assert fake.mutating() == []


def test_create_sets_limits_nesting_and_labels_at_launch(tmp_path):
    fake, inc = make(tmp_path)
    inc.create("devbox-a", "a", "ubuntu/24.04", 3, "6GiB")
    launch = fake.calls[-1]
    assert launch[:3] == ["launch", "images:ubuntu/24.04", "devbox-a"]
    for option in ("limits.cpu=3", "limits.memory=6GiB", "security.nesting=true", "user.devbox.managed=true",
                   "user.devbox.controller=test", "user.devbox.project=a"):
        assert option in launch
    assert inc.is_ours(inc.get("devbox-a"))
    assert inc.limits(inc.get("devbox-a")) == {"cpus": "3", "memory": "6GiB"}


def test_stop_start_delete_only_when_needed(tmp_path):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-a")
    inc.start("devbox-a")  # already running: nothing to do
    assert [c[0] for c in fake.mutating()] == []
    inc.stop("devbox-a")
    inc.stop("devbox-a")
    inc.start("devbox-a")
    inc.delete("devbox-a")
    inc.delete("devbox-a")  # gone already: not an error
    assert [c[0] for c in fake.mutating()] == ["stop", "start", "delete"]


def test_container_name_and_ip():
    assert incus.container_name("crm") == "devbox-crm"
    with pytest.raises(DevboxError, match="too long"):
        incus.container_name("x" * 60)
    nets = {"state": {"network": {"docker0": {"addresses": [{"family": "inet", "scope": "global", "address": "172.17.0.1"}]},
                                  "eth0": {"addresses": [{"family": "inet6", "scope": "global", "address": "fd::1"},
                                                         {"family": "inet", "scope": "global", "address": "10.1.2.3"}]}}}}
    assert incus.Incus.ipv4(nets) == "10.1.2.3"
    assert incus.Incus.ipv4({"state": {"network": {"lo": {}}}}) is None


# how incus is run on the host


def test_stdin_is_redirected_unless_input_is_piped(tmp_path):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-a")
    inc.exec("devbox-a", "echo hi")
    inc.exec("devbox-a", "cat > /f", input="data")
    plain = [s for s in fake.scripts if "exec devbox-a" in s and not s.startswith("[")]
    assert plain[0].endswith("</dev/null") and not plain[1].endswith("</dev/null")
    assert fake.inputs[-1][1] == "data"


def test_sudo_is_used_only_when_the_user_is_not_in_incus_admin(tmp_path):
    for access, prefix in (("sudo", "sudo -n incus"), ("incus", "incus")):
        fake, inc = make(tmp_path, access)
        inc.list_workspaces()
        assert inc.prefix == prefix
        assert any(s.startswith(prefix + " list") for s in fake.scripts)


def test_no_incus_access_explains_what_to_do(tmp_path):
    fake, inc = make(tmp_path, "none")
    with pytest.raises(DevboxError, match="incus-admin"):
        inc.list_workspaces()


def test_provisioning_reuses_cloud_init_and_checks_the_network(tmp_path):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-a")
    inc.provision("devbox-a", "ssh-ed25519 PUB", "PRIVATE\n", "ssh-ed25519 HOST")
    inside = "\n".join(s for s in fake.scripts if s.startswith("[devbox-a]"))
    for needle in ("getent hosts", "/etc/profile.d/devbox.sh", "openssh-server", "get.docker.com", "mise.run",
                   "cloudflared", "authorized_keys", "ssh_host_" + "ed25519_key", "systemctl restart ssh",
                   "hello-world"):
        assert needle in inside
    keys = [i for _, i in fake.inputs]
    assert "ssh-ed25519 PUB\n" in keys and "PRIVATE\n" in keys


def test_provisioning_stops_with_a_firewall_hint_without_internet(tmp_path):
    fake, inc = make(tmp_path)
    fake.add_container("devbox-a")
    fake.exec_fail["getent hosts"] = 1
    with pytest.raises(DevboxError, match="host bootstrap mini"):
        inc.provision("devbox-a", "k", "p", "q")
    assert not any("get.docker.com" in s for s in fake.scripts)


# jump host


def jump(tmp_path):
    return Jump("dev@mini", tmp_path / "kh", "devbox-host-mini", "ssh-ed25519", Path("/k/host"))


def test_remote_without_jump_is_unchanged(tmp_path):
    r = Remote("203.0.113.5", Path("/k/dev"), tmp_path / "kh", pinned=True)
    assert not any("ProxyCommand" in o for o in r.ssh_opts())
    assert r.ssh_cmd("true")[-3:] == ["root@203.0.113.5", "--", "true"]


def test_remote_through_a_jump_host_uses_a_pinned_proxy_command(tmp_path):
    r = Remote("10.0.0.5", Path("/k/dev"), tmp_path / "kh", pinned=True, jump=jump(tmp_path))
    opts = r.ssh_opts()
    proxy = next(o for o in opts if o.startswith("ProxyCommand="))[len("ProxyCommand="):]
    words = shlex.split(proxy)
    assert words[0] == "ssh" and words[-3:] == ["-W", "%h:%p", "dev@mini"]
    assert "StrictHostKeyChecking=yes" in words and "HostKeyAlias=devbox-host-mini" in words
    assert f"UserKnownHostsFile={tmp_path / 'kh'}" in words and "-i" in words
    assert r.ssh_cmd("true")[-3:] == ["root@10.0.0.5", "--", "true"]


def test_every_command_line_that_reaches_the_workspace_goes_through_the_jump(tmp_path):
    r = Remote("10.0.0.5", Path("/k/dev"), tmp_path / "kh", pinned=True, jump=jump(tmp_path))
    for cmd in (r.ssh_cmd("x"), r.ssh_cmd(tty=True), r.forward_cmd([(8080, 5173)])):
        assert any(a.startswith("ProxyCommand=") for a in cmd)
    # rsync is given the same ssh command, quoted as one -e argument
    ssh = shlex.join(["ssh", *r.ssh_opts()])
    assert "ProxyCommand=" in ssh and shlex.split(ssh)[0] == "ssh"
    assert any(a.startswith("ProxyCommand=ssh ") for a in shlex.split(ssh))


def test_jump_without_identity_leaves_key_choice_to_ssh_config(tmp_path):
    j = Jump("dev@mini", tmp_path / "kh", "devbox-host-mini", "ssh-ed25519")
    assert "-i" not in shlex.split(j.proxy_command())


def test_ssh_failure_through_a_jump_host_hints_at_a_deleted_workspace(tmp_path):
    direct = Remote("1.2.3.4", Path("/k"), tmp_path / "kh")
    assert direct.ssh_failure("boom") == "ssh to 1.2.3.4 failed: boom"
    via = Remote("10.0.0.5", Path("/k"), tmp_path / "kh", jump=jump(tmp_path))
    text = via.ssh_failure("")
    assert text.startswith("ssh to 10.0.0.5 failed (through host dev@mini;") and "`devbox status`" in text
