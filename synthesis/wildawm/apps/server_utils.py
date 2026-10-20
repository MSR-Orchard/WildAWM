import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from simpleArgParser import parse_args
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def default_db_path(file: str) -> str:
    return str(Path(file).resolve().with_name("data.db"))


def create_sqlite_app(
    db_path: str,
    base,
    register_routes: Callable,
    *,
    title: str,
    description: str = "",
    version: str = "1.0.0",
) -> FastAPI:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    base.metadata.create_all(engine)

    kwargs = {"title": title, "version": version}
    if description:
        kwargs["description"] = description
    app = FastAPI(**kwargs)
    register_routes(app, session_local)
    return app


@dataclass
class AppServerArgs:
    port: int = 8000
    db: str = ""
    host: str = "127.0.0.1"


def parse_app_server_args(default_db: str) -> AppServerArgs:
    args = parse_args(AppServerArgs)
    if not args.db:
        args.db = default_db
    return args


def run_mcp_server(
    create_app: Callable[[str], FastAPI],
    *,
    server_name: str,
    default_db: str,
) -> None:
    args = parse_app_server_args(default_db)
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)

    app = create_app(args.db)
    FastApiMCP(app).mount_http()
    print(f"{server_name} starting on {args.host}:{args.port}")
    print(f"MCP endpoint: http://{args.host}:{args.port}/mcp")
    uvicorn.run(app, host=args.host, port=args.port)
