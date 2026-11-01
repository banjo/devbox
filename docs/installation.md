# Installation

On the machine where the agent runs:

1. Install the tools: Python 3.11+, [`uv`](https://docs.astral.sh/uv/),
   [`hcloud`](https://github.com/hetznercloud/cli), `ssh` and `rsync`.
2. Create a dedicated Hetzner Cloud project and an API token with read/write access. Save it:

   ```sh
   mkdir -p ~/.config/devbox
   install -m 600 /dev/null ~/.config/devbox/hcloud.token
   $EDITOR ~/.config/devbox/hcloud.token
   ```

3. Write `~/.config/devbox/config.toml`. Only the token file is required; see
   [`examples/config.toml`](../examples/config.toml) for the other keys.

   ```toml
   hcloud_token_file = "~/.config/devbox/hcloud.token"
   ```

4. Install the CLI:

   ```sh
   git clone https://github.com/banjo/devbox && cd devbox
   uv tool install -e .
   ```

5. Install the skill by linking `skills/hetzner-devbox` into your agent's skills folder, for
   example:

   ```sh
   # Hermes
   mkdir -p ~/.hermes/skills/devops
   ln -s "$PWD/skills/hetzner-devbox" ~/.hermes/skills/devops/hetzner-devbox

   # Agents that read ~/.agents/skills
   mkdir -p ~/.agents/skills
   ln -s "$PWD/skills/hetzner-devbox" ~/.agents/skills/hetzner-devbox
   ```

6. Check it works: `devbox doctor` checks the config, local tools, `hcloud`, the token, the SSH key
   and firewall, leftover resources and whether `idle-check` is scheduled, and says how to fix
   what's wrong.

The first `devbox up` creates an SSH key at `~/.ssh/devbox` (if missing), uploads it to Hetzner, and
creates a firewall that allows SSH only from this machine's public IP. Both are named
`devbox-<controller id>`, where the controller id is a random id stored in
`~/.local/state/devbox/controller-id` (or set with `controller_id` in the config).

## Upgrading from a version before controller ids

Upgrading from a version before controller ids: Hetzner allows each public key only once per
project, so if your local key is already registered as the old `devbox` key (labeled
`managed-by=devbox`, no controller label), devbox keeps using that key as it is. If the same public
key is registered under another owner, `up` stops and tells you how to either label that key for
this install or use a different `ssh_key`. The old firewall named `devbox` is no longer used and can
be deleted in the Hetzner console once no server uses it. Running servers keep working.
