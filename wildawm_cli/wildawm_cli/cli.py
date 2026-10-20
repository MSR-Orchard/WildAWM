#!/usr/bin/env python3
"""Unified `wildawm` CLI: the synthesis (`awm`) commands at top level plus the `eval` group."""

import enum
import logging
import sys
from dataclasses import dataclass, field
from enum import auto

from simpleArgParser import parse_args_with_commands

from awmeval.benchmarks import BENCHMARKS
from awmeval.cli import command_prepare, command_run, command_score
from wildawm.cli.awm import COMMANDS as AWM_COMMANDS
from wildawm.cli.awm import HANDLERS as AWM_HANDLERS
from wildawm.cli.awm import Client, die, log
from wildawm.const_config import DASHBOARD as DEFAULT_DASHBOARD

DESCRIPTION = "wildawm — WildAWM unified CLI"


# awm's command groups are spliced in at the top level; `eval` is the only extra group.
class Command(enum.Enum):
    eval = auto()    # evaluation: the out-of-distribution benchmark harness (`awmeval`)


class EvalCmd(enum.Enum):
    run = auto()      # run a benchmark
    score = auto()    # recompute summary.json of a run directory
    prepare = auto()  # download benchmark data
    list = auto()     # list benchmarks


# Mirrors awmeval's argparse surface; parsed instances go straight to `awmeval.cli.command_*`.
@dataclass
class EvalRun:
    benchmark: str  # benchmark name (required): bare name from `wildawm eval list`
    config: list[str] | None = None  # config file(s), comma-separated (awmeval: repeatable -c/--config)
    tasks: str | None = None  # comma-separated task ids
    limit: int | None = None  # run only the first N tasks
    name: str | None = None  # run name; reuse it to resume
    set: list[str] | None = field(default_factory=list)  # config overrides KEY=VALUE, comma-separated (awmeval: repeatable --set)


@dataclass
class EvalScore:
    run_dir: str  # run directory to re-score (required)


@dataclass
class EvalPrepare:
    benchmark: str  # benchmark name (required): bare name from `wildawm eval list`
    config: list[str] | None = None  # config file(s), comma-separated (awmeval: repeatable -c/--config)
    set: list[str] | None = field(default_factory=list)  # config overrides KEY=VALUE, comma-separated (awmeval: repeatable --set)


@dataclass
class EvalList:
    """List the benchmark registry (takes no options)."""


EVAL_TREE = {
    EvalCmd.run: EvalRun,
    EvalCmd.score: EvalScore,
    EvalCmd.prepare: EvalPrepare,
    EvalCmd.list: EvalList,
}

# awm's own tree plus `eval`, so parsed synthesis tuples are `AWM_HANDLERS` keys as-is.
COMMANDS = {
    **AWM_COMMANDS,
    Command.eval: EVAL_TREE,
}


def fix_export_harbor_alias(argv: list[str]) -> None:
    """Rewrite `syn export-harbor` in place to the enum member name `export_harbor`."""
    for i, token in enumerate(argv[:-1]):
        if token == "syn" and argv[i + 1] == "export-harbor":
            argv[i + 1] = "export_harbor"


def parse(argv: list[str] | None = None):
    """Parse `argv` (default: `sys.argv`) into (command tuple, config instance)."""
    if argv is None:
        fix_export_harbor_alias(sys.argv)
        pass_in, disable_cmd = None, False
    else:
        argv = list(argv)
        fix_export_harbor_alias(argv)
        pass_in, disable_cmd = argv, True
    return parse_args_with_commands(
        COMMANDS,
        description=DESCRIPTION,
        pass_in=pass_in,
        disable_cmd=disable_cmd,
    )


def _dispatch_awm(command: tuple, cfg) -> None:
    """Dispatch a synthesis-side command through awm's own handlers."""
    handler = AWM_HANDLERS.get(command)
    if not handler:
        die(f"no handler for command {command}")
    dashboard = getattr(cfg, "dashboard", DEFAULT_DASHBOARD)
    client = Client(dashboard)
    log(f"$ wildawm {' '.join(x.name for x in command)}  (dashboard={dashboard})")
    try:
        handler(client, cfg)
    except RuntimeError as e:
        die(str(e))


def _dispatch_eval(cmd: EvalCmd, cfg) -> None:
    """Dispatch a `wildawm eval ...` command through awmeval's command functions."""
    if cmd is EvalCmd.list:
        print("\n".join(BENCHMARKS))
        return
    if cmd in (EvalCmd.run, EvalCmd.prepare) and cfg.benchmark not in BENCHMARKS:
        print(f"wildawm eval {cmd.name}: unknown benchmark {cfg.benchmark!r}; "
              f"choose from: {', '.join(BENCHMARKS)}", file=sys.stderr)
        raise SystemExit(2)
    # Same logging setup as awmeval.main.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if cmd is EvalCmd.run:
        command_run(cfg)
    elif cmd is EvalCmd.score:
        command_score(cfg)
    elif cmd is EvalCmd.prepare:
        command_prepare(cfg)
    else:
        die(f"no handler for command {cmd}")


def dispatch(command: tuple, cfg) -> None:
    """Run a parsed (command tuple, config instance)."""
    if command[0] is Command.eval:
        return _dispatch_eval(command[1], cfg)
    return _dispatch_awm(command, cfg)


def main() -> None:
    command, cfg = parse()
    dispatch(command, cfg)


if __name__ == "__main__":
    main()
