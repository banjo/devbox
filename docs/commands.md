# Commands

```text
devbox project add NAME --path PATH       register a project
devbox project list                       list projects
devbox project notes NAME                 show the notes on how to run it
devbox project notes NAME --set TEXT      replace them (or --set-file FILE, - for stdin)
devbox project set NAME --idle-timeout D  per-project idle timeout (2h, 1d, off, default)
devbox project remove NAME                hide a project (its cost history is kept)

devbox server-types [filters]             server types that can be ordered now, cheapest first
devbox up NAME [--type T --location L]    reuse the server, or create one (from the latest snapshot
                                          if there is one; --fresh for a clean image), sync code
devbox resume NAME                        like up, but only from a snapshot
    up/resume options: --startup-timeout 5m, --cleanup-on-failure, --fresh, --force, --over-budget
devbox sync NAME [--watch]                copy local code to the server (keep syncing on save with --watch)
devbox pull NAME [PATHS...] [--all]       copy files that differ on the server into a local staging folder
devbox forward NAME PORT...               forward local ports to the server (PORT or LOCAL:REMOTE)
devbox secrets set NAME KEY=VALUE... [--file F] [--from-file F|-]
                                          store env values locally; written to the server now and on every up
devbox secrets list|unset|push NAME       show key names, forget keys, write to the server again
devbox db engines                         supported database engines and their options
devbox db add NAME DB --engine E --option K=V...
                                          describe a project database (postgres, sqlite)
devbox db list|remove NAME [DB]           databases and their latest dumps
devbox db dump NAME [DB] [-o FILE]        dump a database from the server to a local file
devbox db restore NAME [DB] [--file F] --yes
                                          replace a database on the server with a dump
devbox exec NAME [--timeout S] -- CMD...  run a command in /srv/app and wait
devbox exec NAME --detach --name P [--keep-alive] -- CMD
                                          start a background process P (--keep-alive: idle shutdown waits for it)
devbox ps NAME                            list background processes
devbox logs NAME P [-f] [-n LINES]        output from process P
devbox stop NAME P                        stop process P
devbox expose NAME PORT                   public preview URL for PORT; the app finds it in $DEVBOX_PREVIEW_URL
    expose options: --name PREVIEW (several side by side), --protect (password), --wait-for /health, --quick
devbox unexpose NAME [PREVIEW...]         close previews (all of them without names)
devbox ssh NAME                           shell on the server
devbox status [NAME]                      servers, processes, preview URLs, cost so far
devbox pause NAME                         snapshot the server, then delete it (= down --snapshot)
devbox down NAME [--snapshot] [--dump | --no-dump]
                                          delete the server (dumping databases first with --dump); prints cost and
                                          activity. Refuses while a registered database has no dump since the
                                          server was last used, unless --no-dump
    pause/down --at-hour-end                 keep the server until just before its next billing hour, then pause/delete it
devbox touch NAME [--for D | --clear]     record activity so idle shutdown waits; --for 2h holds the server
                                          until then (max 24h), --clear ends the hold
devbox idle-check [--dry-run]             pause/delete servers idle past their timeout (for cron)
devbox autostop enable|disable [--yes]    install/remove the cron entry for idle-check (shows the plan without --yes)
devbox autostop status                    whether idle shutdown really runs, and when each server is checked
devbox snapshot list [NAME]               snapshots with size and cost
devbox snapshot delete IMAGE_ID           delete a snapshot
devbox doctor                             check config, tools, token, SSH key, firewall, leftovers, scheduling
devbox cleanup [--yes]                    list (or delete) unused devbox resources and stale local files
devbox history [NAME] [--limit N]         past servers and snapshots with dates, cost and activity
devbox costs [--month M | --days N | --since D] [--until D] [--project P] [--by project|month|day|type|server]
                                          estimated server + snapshot cost for a period
```

All commands accept `--json`, before or after the command (`devbox --json status`,
`devbox project list --json`). With `--json`, errors are a JSON object on stdout (`error`, plus
`code` where known; usage errors have `code: "usage"` and exit 2) and the exit code is nonzero.
`exec` runs a single argument as a shell script (`devbox exec myapp -- 'npm ci && npm test'`) and
quotes several arguments as one command.
