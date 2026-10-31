"""Database engines for `devbox db dump/restore`.

Each engine turns a database's settings into two remote shell commands:
one that writes a dump to stdout and one that restores a dump from stdin.
The commands run in the project folder on the server. To add an engine,
subclass `Engine`, describe its options and register it in `ENGINES`.
"""

import re
import shlex
from dataclasses import dataclass

from devbox import DevboxError


@dataclass(frozen=True)
class Option:
    name: str
    help: str
    required: bool = False
    default: str | None = None


class Engine:
    name: str = ""
    description: str = ""
    extension: str = "dump"
    options: tuple[Option, ...] = ()

    def validate(self, settings: dict[str, str]) -> dict[str, str]:
        known = {o.name for o in self.options}
        unknown = set(settings) - known
        if unknown:
            raise DevboxError(
                f"unknown option(s) for {self.name}: {', '.join(sorted(unknown))}; "
                f"valid: {', '.join(sorted(known))}"
            )
        result = {}
        for o in self.options:
            value = settings.get(o.name, o.default)
            if value is None or value == "":
                if o.required:
                    raise DevboxError(f"{self.name} needs --option {o.name}=... ({o.help})")
                continue
            result[o.name] = value
        self.check(result)
        return result

    def check(self, settings: dict[str, str]) -> None:
        """Engine-specific validation of already known options."""

    def dump_command(self, settings: dict[str, str]) -> str:
        raise NotImplementedError

    def restore_command(self, settings: dict[str, str]) -> str:
        raise NotImplementedError

    def describe(self, settings: dict[str, str]) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(settings.items()))


IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _in_service(settings: dict[str, str], command: list[str]) -> str:
    """Run a command inside a Docker Compose service, or directly on the server."""
    service = settings.get("service")
    if service:
        return shlex.join(["docker", "compose", "exec", "-T", service, *command])
    return shlex.join(command)


class Postgres(Engine):
    name = "postgres"
    description = "PostgreSQL via pg_dump/pg_restore (custom format), in a Compose service or on the server"
    extension = "pgdump"
    options = (
        Option("database", "database name", required=True),
        Option("service", "Docker Compose service running Postgres (omit to use client tools on the server)"),
        Option("user", "database user", default="postgres"),
        Option("host", "host when not using a service, e.g. localhost"),
        Option("port", "port when not using a service"),
    )

    def check(self, settings):
        for key in ("database", "user", "service", "host"):
            if key in settings and not IDENT_RE.match(settings[key]):
                raise DevboxError(f"invalid postgres {key} '{settings[key]}'")
        if "port" in settings and not settings["port"].isdigit():
            raise DevboxError(f"invalid postgres port '{settings['port']}'")

    def _conn(self, settings) -> list[str]:
        args = ["-U", settings["user"]]
        if not settings.get("service"):
            if settings.get("host"):
                args += ["-h", settings["host"]]
            if settings.get("port"):
                args += ["-p", settings["port"]]
        return args

    def dump_command(self, settings):
        return _in_service(settings, ["pg_dump", *self._conn(settings), "-Fc", "--no-owner", settings["database"]])

    def restore_command(self, settings):
        return _in_service(settings, [
            "pg_restore", *self._conn(settings), "--clean", "--if-exists", "--no-owner",
            "--single-transaction", "-d", settings["database"],
        ])


# SQLite: Ubuntu ships python3, so use its sqlite3 module (no sqlite3 CLI needed).
# The backup API gives a consistent copy even while the app is writing.
# Both scripts report problems with sys.exit("...") (exit 1, message on
# stderr) instead of a traceback.
_SQLITE_DUMP = r"""
import os, sqlite3, sys, tempfile
path = sys.argv[1]
if not os.path.isfile(path):
    sys.exit(f"no database at {path} on the server (nothing was dumped)")
tmp = None
try:
    # Read-only URI: never creates the file, never changes it.
    src = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    src.execute("SELECT count(*) FROM sqlite_master").fetchone()
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    dst.close(); src.close()
    with open(tmp, "rb") as f:
        while chunk := f.read(1 << 20):
            sys.stdout.buffer.write(chunk)
except sqlite3.Error as e:
    sys.exit(f"cannot dump {path}: {e}")
finally:
    if tmp:
        os.unlink(tmp)
"""

_SQLITE_RESTORE = r"""
import os, sqlite3, sys, tempfile
path = sys.argv[1]
folder = os.path.dirname(os.path.abspath(path))
tmp = None
try:
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".sqlite", dir=folder)
    size = 0
    with os.fdopen(fd, "wb") as f:
        while chunk := sys.stdin.buffer.read(1 << 20):
            f.write(chunk)
            size += len(chunk)
    if size == 0:
        sys.exit("the dump is empty; nothing was restored")
    with open(tmp, "rb") as f:
        header = f.read(100)
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        sys.exit("the dump is not a SQLite database; nothing was restored")
    page_size = int.from_bytes(header[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    pages = int.from_bytes(header[28:32], "big")
    # The page count in the header is only valid when these two counters match.
    if pages and header[24:28] == header[92:96] and size < page_size * pages:
        sys.exit(f"the dump is truncated ({size} of {page_size * pages} bytes); nothing was restored")
    src = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    result = [row[0] for row in src.execute("PRAGMA integrity_check").fetchall()]
    if result != ["ok"]:
        sys.exit("the dump failed SQLite's integrity check; nothing was restored")
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close(); src.close()
except sqlite3.Error as e:
    sys.exit(f"restore failed, the database was not changed: {e}")
except OSError as e:
    sys.exit(f"restore failed: {e}")
finally:
    if tmp and os.path.exists(tmp):
        os.unlink(tmp)
"""


class Sqlite(Engine):
    name = "sqlite"
    description = "SQLite database file on the server, copied with the SQLite backup API"
    extension = "sqlite"
    options = (
        Option("path", "database file, relative to the project folder (e.g. data/app.db)", required=True),
    )

    def check(self, settings):
        path = settings["path"]
        if path.startswith("/") or ".." in path.split("/"):
            raise DevboxError(f"invalid sqlite path '{path}': use a path inside the project folder")

    def dump_command(self, settings):
        return shlex.join(["python3", "-c", _SQLITE_DUMP, settings["path"]])

    def restore_command(self, settings):
        return shlex.join(["python3", "-c", _SQLITE_RESTORE, settings["path"]])


ENGINES: dict[str, Engine] = {e.name: e for e in (Postgres(), Sqlite())}


def get_engine(name: str) -> Engine:
    try:
        return ENGINES[name]
    except KeyError:
        raise DevboxError(f"unknown database engine '{name}'; available: {', '.join(sorted(ENGINES))}")


def parse_options(pairs: list[str]) -> dict[str, str]:
    settings = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise DevboxError(f"invalid option '{pair}': use KEY=VALUE")
        settings[key.strip()] = value.strip()
    return settings
