# Databases

- Databases are described per project with an engine and options; `devbox db engines` lists them:
  - `postgres`: `database` (required), `service` (Docker Compose service; omit to use `pg_dump` on
    the server), `compose_file` (Compose file relative to the project when it isn't in the root,
    e.g. `docker/docker-compose.yml`; passed as `docker compose -f`), `user` (default `postgres`),
    `host`, `port`. Dumps use `pg_dump -Fc`; restores use
    `pg_restore --clean --if-exists --single-transaction`.
  - `sqlite`: `path` (relative to the project). Uses the server's `python3` and SQLite's backup API,
    so no `sqlite3` CLI is needed and the copy is consistent while the app runs. Dumping a database
    file that doesn't exist fails instead of creating an empty one. A restore creates missing
    folders, rejects empty, truncated or corrupt input (header size check and
    `PRAGMA integrity_check`) before it replaces anything, and removes its temporary file either
    way.
- Dumps are streamed to `~/.local/state/devbox/dumps/NAME/DB-<time>.<ext>` (files 0600, folders
  0700, regardless of umask); `restore` uses the latest one unless `--file` is given, and always
  needs `--yes`.
- `down` deletes the server's data, so it refuses while a registered database has no dump taken
  since the server was last used (any activity except `db dump` counts). Use `down --dump`,
  `db dump` first, `pause` (keeps everything in the snapshot) or `--no-dump` to delete anyway.
  `down --at-hour-end` checks the same when it is requested.
- More engines are a small class in `src/devbox/databases.py`: options, a dump command that writes
  to stdout and a restore command that reads stdin.
