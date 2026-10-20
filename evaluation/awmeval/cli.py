from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.request
from pathlib import Path

import yaml

from . import runner
from .benchmarks import BENCHMARKS, load
from .config import load_config, wrap


def discover_model(section) -> None:
    """Fill an empty model name from the endpoint's /models list."""
    if section.get("model"):
        return
    request = urllib.request.Request(section.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {section.get('api_key', '')}"})
    models = [item["id"] for item in json.loads(urllib.request.urlopen(request, timeout=30).read())["data"]]
    if len(models) != 1:
        raise SystemExit(f"set the model name explicitly; {section.base_url} serves {models}")
    section.model = models[0]


def select(tasks: list[dict], ids: str | None, limit: int | None) -> list[dict]:
    if ids:
        wanted = [item.strip() for item in ids.split(",") if item.strip()]
        by_id = {str(task["id"]): task for task in tasks}
        missing = [item for item in wanted if item not in by_id]
        if missing:
            raise SystemExit(f"unknown task ids: {missing[:10]}")
        tasks = [by_id[item] for item in wanted]
    return tasks[:limit] if limit else tasks


def default_config() -> list[str]:
    return ["config.yaml"] if Path("config.yaml").exists() else []


def command_run(args) -> None:
    cfg = load_config(args.config or default_config(), args.benchmark, args.set)
    discover_model(cfg.agent)
    bench = load(args.benchmark)(cfg)
    tasks = select(bench.tasks(), args.tasks, args.limit)
    name = args.name or f"{re.sub(r'[^A-Za-z0-9._-]+', '-', cfg.agent.model.split('/')[-1])}-{time.strftime('%Y%m%d-%H%M%S')}"
    out = Path(cfg.run.output_dir) / args.benchmark / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.yaml").write_text(yaml.safe_dump(json.loads(json.dumps(cfg)), sort_keys=False), encoding="utf-8")
    summary = runner.run(bench, tasks, out)
    print(json.dumps(summary, indent=2))


def command_score(args) -> None:
    out = Path(args.run_dir)
    cfg = wrap(yaml.safe_load((out / "config.yaml").read_text(encoding="utf-8")))
    print(json.dumps(runner.finalize(load(cfg.name)(cfg), out), indent=2))


def command_prepare(args) -> None:
    load(args.benchmark)(load_config(args.config or default_config(), args.benchmark, args.set)).prepare()


def main() -> None:
    parser = argparse.ArgumentParser(prog="awmeval", description="WildAWM out-of-distribution evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a benchmark")
    run.add_argument("benchmark", choices=list(BENCHMARKS))
    run.add_argument("-c", "--config", action="append", help="config file, repeatable (default: ./config.yaml)")
    run.add_argument("--tasks", help="comma-separated task ids")
    run.add_argument("--limit", type=int, help="run only the first N tasks")
    run.add_argument("--name", help="run name; reuse it to resume")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a config value, e.g. agent.model=x")
    score = sub.add_parser("score", help="recompute summary.json of a run directory")
    score.add_argument("run_dir")
    prepare = sub.add_parser("prepare", help="download benchmark data")
    prepare.add_argument("benchmark", choices=list(BENCHMARKS))
    prepare.add_argument("-c", "--config", action="append")
    prepare.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    sub.add_parser("list", help="list benchmarks")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.command == "list":
        print("\n".join(BENCHMARKS))
    else:
        {"run": command_run, "score": command_score, "prepare": command_prepare}[args.command](args)


if __name__ == "__main__":
    main()
