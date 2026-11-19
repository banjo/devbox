"""An in-memory own host: answers the ssh commands devbox sends and emulates `incus`."""

import base64
import json
import shlex
import subprocess

# Built at import time (a fixed, meaningless blob) so no key-like literal sits in the source.
FAKE_KEY = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()


def done(out="", err="", code=0):
    return subprocess.CompletedProcess([], code, out, err)


class FakeHost:
    """Use as `HostShell(runner=fake)`. Containers are dicts as `incus list --format json` prints them."""

    def __init__(self, access="sudo", controller="test"):
        self.access = access  # "incus" (in the incus-admin group), "sudo" or "none"
        self.controller = controller
        self.containers: dict[str, dict] = {}
        self.calls: list[list[str]] = []  # incus argument lists, in order
        self.scripts: list[str] = []  # every script, including ones run inside containers
        self.inputs: list[tuple[str, str]] = []
        self.next_ip = 2
        self.exec_fail: dict[str, int] = {}  # substring of an exec script -> exit code
        self.exec_out: dict[str, str] = {}  # substring of an exec script -> stdout
        self.version = "6.0.0"
        self.images: list[dict] = []  # as `incus image list --format json`
        self.launched_from: dict[str, str] = {}

    def add_container(self, name, labels=True, status="Running", controller=None, project=None):
        config = {}
        if labels:
            config = {"user.devbox.managed": "true", "user.devbox.controller": controller or self.controller}
            if project:
                config["user.devbox.project"] = project
        self.containers[name] = {"name": name, "status": status, "config": config, "expanded_config": dict(config),
                                 "state": {"network": {"eth0": {"addresses": [
                                     {"family": "inet", "scope": "global", "address": f"10.0.0.{self.next_ip}"}]}}}}
        self.next_ip += 1

    # runner(cmd, **kw) as subprocess.run
    def __call__(self, cmd, **kw):
        assert cmd[0] == "ssh", cmd
        remote = cmd[cmd.index("--") + 1] if "--" in cmd else ""
        script = shlex.split(remote)[2] if remote.startswith("bash -c") else remote
        self.scripts.append(script)
        if kw.get("input") is not None:
            self.inputs.append((script, kw["input"]))
        if script.startswith("command -v incus"):
            return done({"incus": "incus", "sudo": "sudo", "none": "none"}[self.access] + "\n")
        words = shlex.split(script.replace("</dev/null", ""))
        if words[:3] == ["sudo", "-n", "incus"]:
            words = words[2:]
        if words and words[0] == "incus":
            if self.access == "none":
                return done(err="permission denied", code=1)
            return self.incus(words[1:])
        return done()

    def incus(self, args):
        self.calls.append(args)
        verb = args[0]
        if verb == "list":
            names = [a for a in args[1:] if a.startswith("^")]
            found = [c for c in self.containers.values() if not names or f"^{c['name']}$" == names[0]]
            return done(json.dumps(found))
        if verb == "version":
            return done(f"Client version: {self.version}\nServer version: {self.version}\n")
        if verb == "image":
            if args[1] == "list":
                return done(json.dumps(self.images))
            if args[1] == "delete":
                self.images = [i for i in self.images if i["fingerprint"] != args[2]]
                return done()
        if verb == "publish":
            alias = args[args.index("--alias") + 1]
            props = dict(a.split("=", 1) for a in args[2:] if "=" in a)
            self.images = [i for i in self.images if alias not in [x["name"] for x in i["aliases"]]]
            self.images.append({"fingerprint": f"fp{len(self.images)}{alias[-6:]}", "aliases": [{"name": alias}],
                                "properties": props, "size": 1000})
            return done()
        name = next((a for a in args[1:] if a.startswith("devbox-") and not a.startswith("devbox-base-")), None)
        if verb == "launch":
            self.add_container(name, labels=False)
            c = self.containers[name]
            self.launched_from[name] = args[1]
            c["devices"], c["expanded_devices"] = {}, {}
            for i, opt in enumerate(args):
                if "=" in opt and not opt.startswith("images:") and args[i - 1] != "-d":
                    k, v = opt.split("=", 1)
                    c["config"][k] = v
                    c["expanded_config"][k] = v
                elif args[i - 1] == "-d":
                    dev, _, kv = opt.partition(",")
                    k, v = kv.split("=", 1)
                    c["devices"][dev] = {k: v}
                    c["expanded_devices"][dev] = {"type": "nic", k: v}
            return done()
        if verb == "exec":
            script = args[args.index("bash") + 2]
            self.scripts.append(f"[{name}] {script}")
            for needle, code in self.exec_fail.items():
                if needle in script:
                    return done(err="failed", code=code)
            for needle, out in self.exec_out.items():
                if needle in script:
                    return done(out)
            return done()
        c = self.containers.get(name)
        if verb == "start":
            c["status"] = "Running"
        elif verb == "stop":
            c["status"] = "Stopped"
        elif verb == "delete":
            del self.containers[name]
        elif verb == "config" and args[1] == "device":
            k, v = args[5].split("=", 1)
            c.setdefault("devices", {}).setdefault(args[4], {})[k] = v
            c.setdefault("expanded_devices", {}).setdefault(args[4], {})[k] = v
        elif verb == "config":
            k, v = args[3].split("=", 1)
            c["config"][k] = v
            c["expanded_config"][k] = v
        return done()

    def mutating(self):
        return [c for c in self.calls if c[0] in ("launch", "start", "stop", "delete", "exec", "config", "publish")
                or c[:2] == ["image", "delete"]]
