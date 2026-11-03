"""Tests for issue #38: tell the app its preview URL before it starts."""

import json
import subprocess

from devbox import previews

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_previews_6 import box, cf, expose  # noqa: F401  (fixtures)


def env_on_server(cloud):
    """The variables the preview env file exports, as a login shell would see them."""
    script = cloud.files[previews.ENV_FILE]
    out = subprocess.run(["bash", "-c", script + "\nenv"], capture_output=True, text=True, check=True).stdout
    return {k: v for k, _, v in (line.partition("=") for line in out.splitlines()) if k.startswith("DEVBOX_PREVIEW")}


def test_env_values():
    assert previews.env("crm", None, []) == {}
    assert previews.env("crm", "example.com", []) == {
        "DEVBOX_PREVIEW_DOMAIN": "example.com",
        "DEVBOX_PREVIEW_URL": "https://crm.example.com", "DEVBOX_PREVIEW_HOST": "crm.example.com",
    }
    rows = [{"name": "my-api", "url": "https://crm-my-api.example.com"},
            {"name": "default", "url": "https://random-1.trycloudflare.com"}]
    values = previews.env("crm", "example.com", rows)
    assert values["DEVBOX_PREVIEW_URL"] == "https://random-1.trycloudflare.com"  # what is served wins
    assert values["DEVBOX_PREVIEW_HOST_MY_API"] == "crm-my-api.example.com"


def test_env_script_quotes_values():
    script = previews.env_script({"DEVBOX_PREVIEW_URL": "https://x.example.com/?a=1&b='2'"})
    out = subprocess.run(["bash", "-c", script + 'printf %s "$DEVBOX_PREVIEW_URL"'],
                         capture_output=True, text=True, check=True).stdout
    assert out == "https://x.example.com/?a=1&b='2'"


def test_stable_url_is_known_right_after_up(capsys, box, cloud, cf):
    code, out, err = run(capsys, "up", "crm", "--json")
    assert code == 0, out + err
    assert json.loads(out)["preview_env"]["DEVBOX_PREVIEW_URL"] == "https://crm.example.com"
    assert env_on_server(cloud) == {
        "DEVBOX_PREVIEW_DOMAIN": "example.com",
        "DEVBOX_PREVIEW_URL": "https://crm.example.com", "DEVBOX_PREVIEW_HOST": "crm.example.com",
    }
    assert box.units == {}  # nothing exposed yet

    code, data = expose(capsys, "3000", "--name", "api")
    assert data["preview_env"]["DEVBOX_PREVIEW_URL_API"] == "https://crm-api.example.com"
    assert env_on_server(cloud)["DEVBOX_PREVIEW_HOST_API"] == "crm-api.example.com"

    run(capsys, "unexpose", "crm", "api")
    assert "DEVBOX_PREVIEW_URL_API" not in env_on_server(cloud)
    assert env_on_server(cloud)["DEVBOX_PREVIEW_URL"] == "https://crm.example.com"


def test_quick_urls_are_set_after_expose_and_replaced(capsys, box, cloud):
    run(capsys, "up", "crm")
    assert env_on_server(cloud) == {}

    code, out, _ = run(capsys, "expose", "crm", "5173")
    first = out.splitlines()[0]
    assert env_on_server(cloud)["DEVBOX_PREVIEW_URL"] == first
    assert "$DEVBOX_PREVIEW_URL has this URL" in out and "restart the app" in out

    code, again = expose(capsys, "5173")
    assert again["url"] != first and env_on_server(cloud)["DEVBOX_PREVIEW_URL"] == again["url"]

    run(capsys, "stop", "crm", "tunnel")
    assert env_on_server(cloud) == {}


def test_a_new_server_never_keeps_a_stale_quick_url(capsys, box, cloud):
    run(capsys, "up", "crm")
    expose(capsys, "5173")
    assert env_on_server(cloud)["DEVBOX_PREVIEW_URL"]
    # A restored server has the snapshot's file; `up` rewrites it from the (empty) preview list.
    run(capsys, "pause", "crm")
    run(capsys, "up", "crm")
    assert env_on_server(cloud) == {}
