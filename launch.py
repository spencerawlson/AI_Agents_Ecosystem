#!/usr/bin/env python3
"""One-command launcher for the AI Agents Ecosystem.

    python launch.py                # dashboard on http://0.0.0.0:8000
    python launch.py dashboard      # same, with --port / --host options
    python launch.py worker         # headless agent tick loop
    python launch.py all            # dashboard + worker together
    python launch.py initdb         # create Postgres schema (needs DATABASE_URL)

    python launch.py worker -- --ticks 5 --interval 10
        # pass extra args through to the worker after --

    Postgres (optional): set DATABASE_URL and add --use-postgres
        docker compose -f infra/docker-compose.yml up -d
        export DATABASE_URL=postgresql://ecosystem:ecosystem@localhost:5432/ecosystem
        python launch.py initdb
        python launch.py all --use-postgres
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def cmd_dashboard(args) -> int:
    import uvicorn

    from ecosystem.runtime import build_runtime, create_dashboard_app

    rt = build_runtime(use_postgres=args.use_postgres)
    app = create_dashboard_app(rt)
    print(f"dashboard: http://{args.host}:{args.port} "
          f"(store={'postgres' if args.use_postgres else 'memory'})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(worker_argv: list[str], use_postgres: bool) -> int:
    from ecosystem.worker import main as worker_main

    argv = list(worker_argv)
    if use_postgres and "--use-postgres" not in argv:
        argv.append("--use-postgres")
    return worker_main(argv)


def cmd_all(args, worker_argv: list[str]) -> int:
    thread = threading.Thread(
        target=cmd_worker, args=(worker_argv, args.use_postgres), daemon=True
    )
    thread.start()
    return cmd_dashboard(args)


def cmd_initdb(args) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        print("example: export DATABASE_URL="
              "postgresql://ecosystem:ecosystem@localhost:5432/ecosystem",
              file=sys.stderr)
        return 1
    from sqlalchemy import create_engine

    from infra.db_models import Base

    engine = create_engine(url.replace("+asyncpg", ""))
    Base.metadata.create_all(engine)
    print(f"schema created at {url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch the AI Agents Ecosystem")
    parser.add_argument(
        "command", nargs="?", default="dashboard",
        choices=["dashboard", "worker", "all", "initdb"],
        help="what to run (default: dashboard)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="dashboard bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000,
                        help="dashboard bind port (default: 8000)")
    parser.add_argument("--use-postgres", action="store_true",
                        help="use PostgresTaskStore via DATABASE_URL")
    # Split off worker args manually: everything after a bare -- belongs
    # to the worker. (argparse.REMAINDER would swallow our own flags.)
    argv = sys.argv[1:]
    if "--" in argv:
        idx = argv.index("--")
        main_argv, worker_argv = argv[:idx], argv[idx + 1:]
    else:
        main_argv, worker_argv = argv, []
    args = parser.parse_args(main_argv)

    if args.command == "dashboard":
        return cmd_dashboard(args)
    if args.command == "worker":
        return cmd_worker(worker_argv, args.use_postgres)
    if args.command == "all":
        return cmd_all(args, worker_argv)
    if args.command == "initdb":
        return cmd_initdb(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
