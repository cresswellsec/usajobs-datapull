"""CLI: python -m usajobs_datapull <command>"""
from __future__ import annotations

import argparse
import importlib
import sys

from . import config

# Order matters: `all` runs these in sequence. `codelists` is separate because
# it only needs refreshing occasionally.
PIPELINE = ("fetch", "normalize", "chunk", "embed", "bundle", "publish")
COMMANDS = {
    "check": "show which settings are present (never prints secret values)",
    "codelists": "USAJOBS code lists -> data/reference/codelists/<date>/",
    "fetch": "USAJOBS API -> data/raw/<snapshot>/",
    "normalize": "raw pages -> one clean record per posting (work.db)",
    "chunk": "clean postings -> searchable chunks (work.db)",
    "embed": "chunks -> vectors, cached by content hash + model (work.db)",
    "bundle": "work.db -> dist/bundle/ (built and validated)",
    "publish": "dist/bundle/ -> Hugging Face dataset",
    "probe": "measure the USAJOBS API's limits -> data/reference/api-probes/",
    "remote":"show the published dataset: private?, latest bundle, tags, anonymous access blocked?",
    "all": "fetch, normalize, chunk, embed, bundle, publish",
}


def check() -> None:
    """Report each setting as set/missing. Secret values are never printed."""
    width = max(len(n) for n in config.SETTINGS)
    for name in config.SETTINGS:
        value = getattr(config, name)
        if value is None or value == "":
            shown = "MISSING"
        elif name in config.SECRETS:
            shown = "set"
        elif isinstance(value, bool):
            shown = str(value).lower()
        else:
            shown = value
        print(f"{name:<{width}}  {shown}")


def run_stage(name: str) -> None:
    module = importlib.import_module(f".{name}", __package__)
    try:
        module.run()
    except NotImplementedError as e:
        print(f"[{name}] not implemented yet: {e}", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m usajobs_datapull")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        if name == "probe":
            p.add_argument("--burst", type=int, default=0, metavar="N",
                           help=f"also send N requests with no delay (max {config.PROBE_MAX_BURST})")
            p.add_argument("--rate", action="store_true",
                           help=f"also step up through {config.PROBE_RATE_STEPS} requests/second, "
                                f"{config.PROBE_RATE_STEP_REQUESTS} each, stopping at the first throttle")
    args = parser.parse_args(argv)

    if args.command == "check":
        check()
    elif args.command == "probe":
        from . import probe
        probe.run(burst=args.burst, rate=args.rate)
    elif args.command == "all":
        for stage in PIPELINE:
            run_stage(stage)
    else:
        run_stage(args.command)


if __name__ == "__main__":
    main()
