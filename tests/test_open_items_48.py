"""Open items of issue #48: parallel `up`, safer `host remove --force`, the runc note on cached images,
bootstrap wording and the heads-up before a base image rebuild. (The exec result retry is in test_remote.py.)"""

import contextlib
import fcntl
import json
import os
import threading
import time

import pytest

from devbox import DevboxError, cli, config, db, hosts, incus, startup
from fake_host import done
from test_hosts import facts
from test_workspaces import add_host, run, world  # noqa: F401  (world is a fixture)


def add_project(capsys, world, name):
    (world.tmp / name).mkdir(exist_ok=True)
    assert run(capsys, "project", "add", name, "--path", str(world.tmp / name))[0] == 0


def seed_base_image(world, workaround=False):
    alias = incus.base_alias("ubuntu/24.04", world.version)
    props = {"user.devbox.base": "true", "user.devbox.controller": world.controller}
    if workaround:
        props[incus.WORKAROUND_PROP] = "true"
    world.images.append({"fingerprint": "fpseed", "aliases": [{"name": alias}], "properties": props, "size": 1})
    return alias


def state_lock_is_free(world):
    with open(world.tmp / "state" / "devbox" / "lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def in_threads(*calls):
    """Run each callable in its own thread; returns their results (or exceptions) in order."""
    results = [None] * len(calls)

    def target(i, call):
        try:
            results[i] = call()
        except BaseException as e:  # noqa: BLE001  (reported to the test)
            results[i] = e

    threads = [threading.Thread(target=target, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


# --- 1. parallel up -------------------------------------------------------------------------------


def test_workspaces_of_different_projects_start_in_parallel(capsys, world):
    add_host(capsys, world)
    add_project(capsys, world, "docs")
    seed_base_image(world)
    meeting = threading.Barrier(3, timeout=20)  # all three must be inside their startup at the same time
    real = world.incus

    def incus_(args):
        if args[0] == "exec" and "authorized_keys &&" in args[-1]:
            meeting.wait()  # a serialized `up` never lets the others arrive: BrokenBarrierError
        return real(args)

    world.incus = incus_
    results = in_threads(*[lambda n=n: cli.main(["up", n, "--host", "mini"]) for n in ("crm", "blog", "docs")])
    assert results == [0, 0, 0], results
    conn = db.connect(config.load().db_path)
    assert {n: db.active_server(conn, n)["status"] for n in ("crm", "blog", "docs")} == dict.fromkeys(
        ("crm", "blog", "docs"), "running")
    assert all(world.launched_from[f"devbox-{n}"] == incus.base_alias("ubuntu/24.04", "6.0.0")
               for n in ("crm", "blog", "docs"))


def test_the_state_lock_is_not_held_while_a_workspace_starts(capsys, world):
    add_host(capsys, world)
    seen = []
    real = world.incus

    def incus_(args):
        if args[0] in ("launch", "publish") or (args[0] == "exec" and "get.docker.com" in args[-1]):
            seen.append(state_lock_is_free(world))
        return real(args)

    world.incus = incus_
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert seen and all(seen)


def test_the_base_image_is_built_once_and_the_others_start_from_it(capsys, world):
    add_host(capsys, world)
    add_project(capsys, world, "docs")
    real = world.incus
    building = []

    def incus_(args):
        if args[0] == "publish":
            building.append(1)
            time.sleep(1.0)  # the others are by now waiting for the lock
        return real(args)

    world.incus = incus_
    results = in_threads(*[lambda n=n: cli.main(["up", n, "--host", "mini", "--json"]) for n in ("crm", "blog", "docs")])
    assert results == [0, 0, 0], results
    err = capsys.readouterr().err
    assert len(building) == 1 and len([c for c in world.calls if c[0] == "publish"]) == 1
    alias = incus.base_alias("ubuntu/24.04", "6.0.0")
    sources = sorted(world.launched_from[f"devbox-{n}"] for n in ("crm", "blog", "docs"))
    assert sources == sorted([alias, alias, "images:ubuntu/24.04"])  # exactly one cold start
    assert err.count("is building the base image on host mini") >= 1


def test_two_ups_of_the_same_project_take_turns(capsys, world):
    add_host(capsys, world)
    real = world.incus
    inside = []
    overlap = []

    def incus_(args):
        if args[0] == "exec" and "authorized_keys &&" in args[-1]:
            inside.append(1)
            if len(inside) > 1:
                overlap.append(True)
            time.sleep(1.0)
            inside.pop()
        return real(args)

    world.incus = incus_
    results = in_threads(lambda: cli.main(["up", "crm", "--host", "mini"]),
                         lambda: (time.sleep(0.4), cli.main(["up", "crm", "--host", "mini"]))[1])
    assert results == [0, 0], results
    assert not overlap
    assert "waiting for another devbox command on crm to finish" in capsys.readouterr().err
    assert len([c for c in world.calls if c[0] == "launch"]) == 1  # the second `up` finds the workspace


def test_down_waits_for_an_up_of_the_same_project(capsys, world):
    add_host(capsys, world)
    real = world.incus
    started = threading.Event()

    def incus_(args):
        if args[0] == "exec" and "authorized_keys &&" in args[-1]:
            started.set()
            time.sleep(1.0)
        return real(args)

    world.incus = incus_
    results = in_threads(lambda: cli.main(["up", "crm", "--host", "mini"]),
                         lambda: (started.wait(10), cli.main(["down", "crm", "--no-dump"]))[1])
    assert results == [0, 0], results
    assert "devbox-crm" not in world.containers  # the delete came after the setup, not in the middle of it
    assert "waiting for another devbox command on crm to finish" in capsys.readouterr().err


def test_hetzner_up_still_holds_the_state_lock_for_the_whole_startup(capsys, world, monkeypatch):
    # max_servers / --additional count existing servers, so two server creations must not overlap.
    monkeypatch.setattr(cli, "resolve_ssh_allow", lambda entries: ["192.0.2.1/32"])
    monkeypatch.setattr(cli.Remote, "wait_cloud_init", lambda self, deadline, progress, secrets=(): [])
    add_project(capsys, world, "crm")
    seen = []
    real = world.hetzner.create_server

    def create(*a, **kw):
        seen.append(state_lock_is_free(world))
        return real(*a, **kw)

    monkeypatch.setattr(world.hetzner, "create_server", create)
    code, out, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    assert code == 0, err
    assert seen == [False]


def test_a_lock_wait_stops_at_the_startup_deadline(tmp_path):
    path = tmp_path / "locks" / "x.lock"
    path.parent.mkdir()
    with open(path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        deadline = startup.Deadline(0.3)
        said = []
        with pytest.raises(startup.StageTimeout):
            with cli.file_lock(path, "waiting", deadline=deadline, stage="create", say=said.append):
                pass
        assert said == ["waiting"]


def test_project_locks_exclude_the_same_project_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    import argparse

    ctx = cli.Context(argparse.Namespace())
    order = []

    def hold(name, label, seconds):
        with ctx.project_lock(name):
            order.append(f"{label} in")
            time.sleep(seconds)
            order.append(f"{label} out")

    a = threading.Thread(target=hold, args=("p1", "a", 0.6))
    a.start()
    time.sleep(0.2)
    b = threading.Thread(target=hold, args=("p2", "b", 0.0))  # another project: no wait
    c = threading.Thread(target=hold, args=("p1", "c", 0.0))  # same project: waits for a
    b.start()
    c.start()
    for t in (a, b, c):
        t.join(10)
    assert order.index("b out") < order.index("a out") < order.index("c in")


def test_a_failed_base_build_hands_the_job_to_the_next_workspace(capsys, world):
    add_host(capsys, world)
    real = world.incus
    failed = []

    def incus_(args):
        if args[0] == "exec" and "get.docker.com" in args[-1] and not failed:
            failed.append(1)
            time.sleep(0.6)
            return done(err="boom", code=1)
        return real(args)

    world.incus = incus_
    results = in_threads(lambda: cli.main(["up", "crm", "--host", "mini"]),
                         lambda: (time.sleep(0.2), cli.main(["up", "blog", "--host", "mini"]))[1])
    assert results[0] == 1 and results[1] == 0, results
    assert len([c for c in world.calls if c[0] == "publish"]) == 1  # blog built and published it


# --- 2. host remove --force and databases -----------------------------------------------------------


def with_database(capsys, world, monkeypatch, project="crm"):
    monkeypatch.setattr(cli.Remote, "stream_out", lambda r, script, dest: dest.write_bytes(b"D") or 1)
    assert run(capsys, "db", "add", project, "main", "--engine", "sqlite", "--option", "path=app.db")[0] == 0


def test_host_remove_force_refuses_a_workspace_with_an_undumped_database(capsys, world, monkeypatch):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert run(capsys, "up", "blog", "--host", "mini")[0] == 0
    with_database(capsys, world, monkeypatch)
    code, _, err = run(capsys, "host", "remove", "mini", "--force")
    assert code == 1 and "crm: main" in err and "--dump" in err and "--no-dump" in err
    assert "blog:" not in err
    assert set(world.containers) == {"devbox-crm", "devbox-blog"}  # nothing was deleted, not even blog
    assert run(capsys, "host", "list")[1].startswith("mini")


def test_host_remove_force_after_a_fresh_dump_or_with_no_dump(capsys, world, monkeypatch):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    with_database(capsys, world, monkeypatch)
    assert run(capsys, "db", "dump", "crm")[0] == 0
    code, out, err = run(capsys, "host", "remove", "mini", "--force")
    assert code == 0, err
    assert "devbox-crm" not in world.containers

    assert run(capsys, "host", "add", "mini", "--ssh", "dev@mini", "--yes")[0] == 0
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    for dump in (world.tmp / "state").rglob("main-*"):  # the dump is older than this workspace's last use
        os.utime(dump, (1, 1))
    code, out, err = run(capsys, "host", "remove", "mini", "--force")
    assert code == 1 and "no dump since" in err
    code, out, err = run(capsys, "host", "remove", "mini", "--force", "--no-dump")
    assert code == 0, err
    assert "devbox-crm" not in world.containers


def test_host_remove_force_dump_dumps_before_deleting(capsys, world, monkeypatch):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    with_database(capsys, world, monkeypatch)
    code, out, err = run(capsys, "host", "remove", "mini", "--force", "--dump", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert [d["database"] for d in data["dumps"]] == ["main"] and data["deleted_workspaces"] == ["crm"]
    from pathlib import Path

    assert Path(data["dumps"][0]["path"]).read_bytes() == b"D"
    assert "devbox-crm" not in world.containers


def test_host_remove_force_dump_needs_a_running_workspace(capsys, world, monkeypatch):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    with_database(capsys, world, monkeypatch)
    assert run(capsys, "pause", "crm")[0] == 0
    code, _, err = run(capsys, "host", "remove", "mini", "--force", "--dump")
    assert code == 1 and "is not running" in err and "devbox up crm" in err
    assert "devbox-crm" in world.containers


def test_host_remove_force_without_databases_is_unchanged(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    code, out, err = run(capsys, "host", "remove", "mini", "--force")
    assert code == 0, err
    assert "deleted workspaces crm" in out


# --- 4. runc workaround on the cached image ------------------------------------------------------------


def test_the_published_image_records_that_the_runc_workaround_was_applied(capsys, world):
    add_host(capsys, world)
    world.exec_out["hello-world"] = incus.WORKAROUND_MARKER + "\n"
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert world.images[0]["properties"][incus.WORKAROUND_PROP] == "true"


def test_every_up_from_a_cached_image_with_the_workaround_prints_the_warning(capsys, world):
    add_host(capsys, world)
    seed_base_image(world, workaround=True)
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["image_cache"] == "hit"
    assert any("runc 1.2.3 workaround" in w and "--zabbly" in w for w in data["warnings"])
    code, out, err = run(capsys, "up", "blog", "--host", "mini")
    assert code == 0 and "runc 1.2.3 workaround" in out


def test_a_cached_image_without_the_workaround_says_nothing(capsys, world):
    add_host(capsys, world)
    seed_base_image(world)
    code, out, err = run(capsys, "up", "crm", "--host", "mini", "--json")
    assert code == 0 and not any("runc" in w for w in json.loads(out)["warnings"])


def test_an_image_made_without_the_workaround_does_not_get_the_property(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    assert incus.WORKAROUND_PROP not in world.images[0]["properties"]


def test_images_from_before_the_property_existed_are_rebuilt():
    # The cache key changed with the property, so an older image (which cannot say whether it carries the
    # workaround) is never used silently.
    assert incus.BASE_CACHE_FORMAT != "1"


# --- 5. bootstrap wording --------------------------------------------------------------------------------


def test_bootstrap_does_not_say_nothing_to_do_next_to_manual_items():
    plan = hosts.make_plan("h", facts(incus_version="7.6", keys_maxkeys="200", ufw_status="", forward_policy=""))
    first = plan.lines()[0]
    assert plan.nothing_to_do and plan.manual
    assert "nothing to do" not in first
    assert "nothing left to change" in first and "1 item below needs you" in first
    two = hosts.make_plan("h", facts(incus_version="7.6", keys_maxkeys="200", ufw_status="", forward_policy="DROP"))
    assert "2 items below need you" in two.lines()[0]


def test_bootstrap_says_nothing_to_do_only_when_there_is_nothing_for_a_human_either():
    plan = hosts.make_plan("h", facts(incus_version="7.6", ufw_status="", forward_policy=""))
    assert not plan.manual and plan.lines()[0].endswith("nothing to do")


# --- 6. heads-up before a base image rebuild -----------------------------------------------------------


def test_up_says_before_building_the_first_base_image_that_this_start_is_cold(capsys, world):
    add_host(capsys, world)
    code, out, err = run(capsys, "up", "crm", "--host", "mini")
    assert code == 0, err
    assert "has no cached base image yet" in err and "cold one" in err and "1 to 3 minutes" in err
    assert err.index("cold one") < err.index("Setting up the workspace")
    assert "cold one" not in run(capsys, "up", "blog", "--host", "mini")[2]  # from the cache: no warning


def test_up_says_why_a_rebuild_is_cold_after_the_incus_version_changed(capsys, world):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    world.version = "7.6"
    code, out, err = run(capsys, "up", "blog", "--host", "mini")
    assert code == 0, err
    assert "cached base image on host mini is outdated" in err and "Incus version" in err and "cold one" in err


def test_bootstrap_upgrade_tells_that_the_next_up_rebuilds_the_base_image(capsys, world, monkeypatch):
    add_host(capsys, world)
    assert run(capsys, "up", "crm", "--host", "mini")[0] == 0
    old = facts(incus_version="6.0.0", codename="noble", arch="amd64", zabbly_repo="0", ufw_status="",
                forward_policy="")
    new = facts(incus_version="7.6", ufw_status="", forward_policy="")
    monkeypatch.setattr(hosts, "collect_facts", lambda shell: dict(old))
    monkeypatch.setattr(hosts, "apply_plan", lambda shell, plan, say=None: dict(new))
    code, out, err = run(capsys, "host", "bootstrap", "mini", "--zabbly", "--yes", "--json")
    assert code == 0, err
    note = json.loads(out)["base_image_note"]
    assert "old Incus version" in note and "cold one" in note and "1 to 3 minutes" in note
    code, out, err = run(capsys, "host", "bootstrap", "mini", "--zabbly", "--yes")
    assert "note: the cached base image on host mini" in out


def test_bootstrap_without_an_incus_change_or_without_a_cached_image_has_no_note(capsys, world, monkeypatch):
    add_host(capsys, world)
    old = facts(incus_version="6.0.0", codename="noble", arch="amd64", zabbly_repo="0", ufw_status="",
                forward_policy="")
    new = facts(incus_version="7.6", ufw_status="", forward_policy="")
    monkeypatch.setattr(hosts, "collect_facts", lambda shell: dict(old))
    monkeypatch.setattr(hosts, "apply_plan", lambda shell, plan, say=None: dict(new))
    code, out, err = run(capsys, "host", "bootstrap", "mini", "--zabbly", "--yes", "--json")
    assert code == 0 and json.loads(out)["base_image_note"] is None  # no cached image on the host
