# Development loop

- `sync --watch` checks the files sync would send every second (`--interval`), waits until a burst
  of saves is over, and syncs. Every sync counts as activity for idle shutdown. Stop it with Ctrl-C.
- `pull` lists the files on the server (skipping `.git`, `node_modules`, virtualenvs and caches),
  drops what Git ignores unless `--all` or explicit paths are given, and copies the ones whose
  content differs from the local copy into `~/.local/state/devbox/pulls/NAME/<time>/`. Nothing in
  the project is changed: review with `diff -ru`, then copy what you want. It also lists tracked
  local files that don't exist on the server.
- `forward NAME 5432 15432:5432` runs SSH port forwarding bound to `127.0.0.1` until Ctrl-C, for
  local database tools and debuggers.
- Secrets live in `~/.config/devbox/secrets/NAME.json` (mode 0600, folder 0700). They are **not
  encrypted**; like SSH keys they rely on file permissions. Values are never printed. `up` and
  `secrets set` write them into the chosen env files on the server, replacing only those keys and
  keeping every other line. If a key appears more than once in the file, the duplicates are removed
  so the new value is the one that counts. Errors about malformed input name the line or argument
  but never repeat its content. Prefer `--from-file -` over `KEY=VALUE` arguments so values don't
  end up in shell history. `project remove` deletes a project's secrets.
