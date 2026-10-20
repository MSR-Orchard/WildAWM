from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import shlex
import subprocess
import tarfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .base import RUN_DIR, Sandbox, SandboxError

METADATA = {"LABEL", "EXPOSE", "MAINTAINER", "STOPSIGNAL", "VOLUME", "HEALTHCHECK"}
VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
BUILD_LOCKS: dict[str, threading.Lock] = {}


@dataclass
class Image:
    """Runtime settings of a Dockerfile image: env the backend does not apply itself, workdir, user, start command."""

    base: str
    env: dict = field(default_factory=dict)
    workdir: str | None = None
    user: str | None = None
    entrypoint: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)

    @property
    def command(self) -> list[str]:
        shell_form = self.entrypoint[:2] == ["/bin/sh", "-c"]
        return self.entrypoint if shell_form else [*self.entrypoint, *self.cmd]


def recipe(dockerfile: Path) -> tuple[str, list[tuple[str, str]]]:
    """Base image and the (KEYWORD, arguments) steps after FROM, with comments and line continuations resolved."""
    steps, pending = [], ""
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.endswith("\\"):
            pending += text[:-1] + " "
            continue
        keyword, *args = (pending + text).split(None, 1)
        steps.append((keyword.upper(), args[0].strip() if args else ""))
        pending = ""
    froms = [index for index, (keyword, _) in enumerate(steps) if keyword == "FROM"]
    if len(froms) != 1 or froms[0] != 0:
        raise SandboxError(f"{dockerfile}: expected one leading FROM (multi-stage builds are not supported)")
    return steps[0][1].split()[0], steps[1:]


def exec_form(args: str) -> list[str]:
    try:
        value = json.loads(args)
    except ValueError:
        value = None
    return [str(item) for item in value] if isinstance(value, list) else ["/bin/sh", "-c", args]


def expand(value: str, env: dict) -> str:
    return VARIABLE.sub(lambda match: str(env.get(match.group(1) or match.group(2), "")), value)


def parse_env(args: str, env: dict) -> dict:
    key, *rest = args.split(None, 1)
    if "=" not in key:
        return {key: expand(rest[0].strip() if rest else "", env)}
    return {name: expand(value, env) for name, _, value in (token.partition("=") for token in shlex.split(args))}


def as_root(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def copy_archive(context: Path, args: str, workdir: str | None) -> bytes:
    """tar.gz rooted at / that reproduces one COPY instruction (Docker source/destination rules, root-owned)."""
    words = json.loads(args) if args.startswith("[") else shlex.split(args)
    if any(word.startswith("--") for word in words):
        raise SandboxError(f"unsupported COPY flags: {args}")
    *sources, dest = words
    dest = posixpath.join(workdir or "/", dest)
    into_directory = dest.endswith("/") or len(sources) > 1
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for pattern in sources:
            matches = sorted(context.glob(pattern)) if any(char in pattern for char in "*?[") else [context / pattern]
            if not matches or not matches[0].exists():
                raise SandboxError(f"COPY source not found in {context}: {pattern}")
            for source in matches:
                if source.is_dir():
                    for child in sorted(source.rglob("*")):
                        archive.add(child, posixpath.join(dest, child.relative_to(source).as_posix()).lstrip("/"), recursive=False, filter=as_root)
                else:
                    archive.add(source, (posixpath.join(dest, source.name) if into_directory else dest).lstrip("/"), filter=as_root)
    return buffer.getvalue()


def apply(base: str, steps: list[tuple[str, str]], sandbox: Sandbox | None = None, context: Path | None = None, timeout: int = 3600) -> Image:
    """Track ENV/WORKDIR/USER/ENTRYPOINT/CMD; with a sandbox, also execute RUN and COPY in order."""
    image = Image(base)
    inherited = {}
    if sandbox is not None:
        inherited = dict(line.split("=", 1) for line in sandbox.exec("env", timeout=60).check("read image env").stdout.splitlines() if re.match(r"\w+=", line))
    for keyword, args in steps:
        if keyword == "ENV":
            image.env.update(parse_env(args, {**inherited, **image.env}))
        elif keyword == "WORKDIR":
            image.workdir = posixpath.join(image.workdir or "/", expand(args, {**inherited, **image.env}))
            if sandbox is not None:
                sandbox.exec(["mkdir", "-p", image.workdir], timeout=60).check(f"WORKDIR {args}")
        elif keyword == "USER":
            image.user = args
        elif keyword == "ENTRYPOINT":
            image.entrypoint = exec_form(args)
        elif keyword == "CMD":
            image.cmd = exec_form(args)
        elif keyword == "RUN":
            if sandbox is not None:
                script = shlex.join(exec_form(args)) if args.startswith("[") else args
                sandbox.exec(script, timeout=timeout, env=image.env, cwd=image.workdir, user=image.user).check(f"RUN {args[:200]}")
        elif keyword == "COPY":
            if sandbox is not None:
                staged = f"{RUN_DIR}/copy-{uuid.uuid4().hex}.tgz"
                sandbox.write(staged, copy_archive(context, args, image.workdir))
                sandbox.exec(f"tar -xzf {staged} -C / && rm -f {staged}", timeout=timeout).check(f"COPY {args}")
        elif keyword not in METADATA:
            raise SandboxError(f"unsupported Dockerfile instruction: {keyword}")
    return image


def content_tag(context: Path, repository: str) -> str:
    """Image tag derived from every file path, mode, and byte of a build context."""
    digest = hashlib.sha256()
    for path in sorted(item for item in context.rglob("*") if item.is_file()):
        with path.open("rb") as stream:
            content = hashlib.file_digest(stream, "sha256").hexdigest()
        digest.update(f"{path.relative_to(context).as_posix()}\0{path.stat().st_mode & 0o7777:o}\0{content}\n".encode())
    return f"{repository}:{digest.hexdigest()[:16]}"


def build_image(context: Path, repository: str, timeout: int = 3600) -> str:
    """`docker build` a context once per content hash and return the image tag."""
    tag = content_tag(context, repository)
    with BUILD_LOCKS.setdefault(tag, threading.Lock()):
        if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
            done = subprocess.run(["docker", "build", "-q", "--label", "awmeval=1", "-t", tag, str(context)], capture_output=True, text=True, timeout=timeout)
            if done.returncode != 0:
                raise SandboxError(f"docker build failed for {context}: {done.stderr.strip()[-3000:]}")
    return tag
