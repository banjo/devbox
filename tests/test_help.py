"""`devbox --help`: every command is grouped and described, every argument has help."""

import argparse

import pytest

from devbox import cli

from test_cli import isolated, run  # noqa: F401  (fixtures)


def subparsers(parser):
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def all_commands(parser, path=()):
    action = subparsers(parser)
    for name, p in (action.choices.items() if action else []):
        yield path + (name,), p
        yield from all_commands(p, path + (name,))


def test_every_command_has_a_description_and_every_argument_help():
    missing = []
    for path, p in all_commands(cli.build_parser()):
        if not p.description:
            missing.append(f"{' '.join(path)}: description")
        for a in p._actions:
            if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            if not a.help:
                missing.append(f"{' '.join(path)}: {a.dest}")
    assert missing == []


def test_command_groups_cover_every_command_once():
    top = subparsers(cli.build_parser()).choices
    grouped = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert sorted(grouped) == sorted(top)


def test_main_help_lists_groups_config_and_docs(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: devbox [-h] [--json] COMMAND ...")
    for title, names in cli.COMMAND_GROUPS:
        assert f"\n{title.lower()}:\n" in out
        for name in names:
            assert f"\n  {name} " in out
    assert "config.toml" in out and "max_servers" in out and cli.DOCS_URL in out
    assert all(len(line) <= 80 for line in out.splitlines() if "http" not in line and "config:" not in line)
    assert "--json" in out.splitlines()[-1]


def test_nested_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["db", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: devbox db [-h] [--json] COMMAND ...")
    assert "\ncommands:\n  engines" in out and "  restore  replace a database" in out

    with pytest.raises(SystemExit):
        cli.main(["db", "dump", "--help"])
    assert capsys.readouterr().out.startswith("usage: devbox db dump ")
