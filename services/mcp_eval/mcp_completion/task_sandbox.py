"""Docker lifecycle for fixture-injected disposable MCP environments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import httpx
from dotenv import load_dotenv

from .docker_http import docker_post_json
from .runtime_log import container_log_path, write_runtime_event
from .task_data import (
    TaskDataFixture,
    prepare_task_workspace,
)

load_dotenv()


class TaskSandboxError(RuntimeError):
    pass


DEFAULT_RUNTIME_IMAGE = "mcp-atlas-runtime:latest"
RUNTIME_DATA_CONTRACT = "external-data-v1"


def _safe_fragment(value: str, limit: int = 30) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.").lower()
    return (value or "task")[:limit]


def _owner_label() -> str:
    configured = os.getenv("MCP_SANDBOX_OWNER")
    raw = configured or f"{socket.gethostname()}-{os.getenv('PORT', '3000')}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{_safe_fragment(raw, 28)}-{digest}"


async def _run(
    *args: str,
    timeout: float = 120.0,
    check: bool = True,
) -> tuple[str, str, int]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TaskSandboxError(
            f"Required executable is unavailable: {args[0]}"
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise TaskSandboxError(
            f"Command timed out after {timeout:.0f}s: {' '.join(args[:3])}"
        ) from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    if check and process.returncode != 0:
        detail = stderr or stdout or f"exit {process.returncode}"
        raise TaskSandboxError(f"{' '.join(args[:3])} failed: {detail}")
    return stdout, stderr, process.returncode


async def inspect_mongo_fixture_image(image: str) -> dict[str, str]:
    stdout, _, _ = await _run(
        "docker",
        "image",
        "inspect",
        image,
        "--format",
        "{{.Id}}\n{{json .Config.Labels}}",
        timeout=30,
    )
    image_id, _, labels_text = stdout.partition("\n")
    labels = json.loads(labels_text or "{}") or {}
    fixture_id = str(labels.get("mcp-atlas.fixture-id") or "")
    logical_database = str(
        labels.get("mcp-atlas.logical-database") or ""
    )
    content_sha256 = str(
        labels.get("mcp-atlas.fixture-sha256") or ""
    )
    if (
        not fixture_id
        or logical_database != "store"
        or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
    ):
        raise TaskSandboxError(
            f"Mongo fixture image {image!r} does not implement the synthetic "
            "store fixture contract"
        )
    return {
        "image": image,
        "image_id": image_id,
        "fixture_id": fixture_id,
        "logical_database": logical_database,
        "content_sha256": content_sha256,
    }


async def inspect_runtime_image(image: str) -> dict[str, str]:
    stdout, _, _ = await _run(
        "docker",
        "image",
        "inspect",
        image,
        "--format",
        "{{.Id}}\n{{json .Config.Labels}}\n{{json .Config.Volumes}}",
        timeout=30,
    )
    image_id, _, remainder = stdout.partition("\n")
    labels_text, _, volumes_text = remainder.partition("\n")
    try:
        labels = json.loads(labels_text or "{}") or {}
        volumes = json.loads(volumes_text or "{}") or {}
    except json.JSONDecodeError as exc:
        raise TaskSandboxError(
            f"invalid runtime image metadata for {image!r}"
        ) from exc
    if (
        labels.get("mcp-atlas.runtime") != "true"
        or labels.get("mcp-atlas.data-contract") != RUNTIME_DATA_CONTRACT
        or labels.get("mcp-atlas.contains-fixture") != "false"
        or "/data" not in volumes
    ):
        raise TaskSandboxError(
            f"runtime image {image!r} does not implement the fixture-free "
            f"{RUNTIME_DATA_CONTRACT} contract"
        )
    return {
        "image": image,
        "image_id": image_id,
        "runtime_version": str(
            labels.get("mcp-atlas.runtime-version") or ""
        ),
        "data_contract": RUNTIME_DATA_CONTRACT,
    }


@dataclass
class ManagedContainer:
    kind: str
    name: str
    task_id: str
    enabled_servers: tuple[str, ...] = ()
    url: Optional[str] = None


# Names of docker resources this process still owns. A sandbox claims a name
# before `docker run` and releases it only once close() has finished, so the
# periodic reaper can tell "in use" from "orphaned" without guessing.
_LIVE_SANDBOX_NAMES: set[str] = set()


def _claim_sandbox_name(name: str) -> None:
    _LIVE_SANDBOX_NAMES.add(name)


def _release_sandbox_names(names: Iterable[str]) -> None:
    _LIVE_SANDBOX_NAMES.difference_update(names)


def _parse_docker_timestamp(value: str) -> Optional[float]:
    """Parse a docker RFC3339 timestamp into an epoch value.

    Docker emits nanosecond precision, which datetime.fromisoformat rejects,
    so the fraction is clipped to microseconds first. Unparseable input yields
    None and callers treat that as "age unknown" (never reaped).
    """
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


@dataclass
class TaskSandbox:
    task_id: str
    local_servers: set[str]
    network_servers: set[str]
    agent_image: str
    mongo_image: str
    startup_timeout: float
    memory_limit: str
    cpu_limit: str
    task_data_source: str
    owner: str = field(default_factory=_owner_label)
    mongo_socket_volume: Optional[str] = None
    mongo_fixture: Optional[dict[str, str]] = None
    runtime_image: Optional[dict[str, str]] = None
    task_data_fixture: Optional[TaskDataFixture] = None
    task_data_dir: Optional[Path] = None
    task_workspace: Optional[Path] = None
    git_repositories: list[str] = field(default_factory=list)
    containers: list[ManagedContainer] = field(default_factory=list)
    owned_names: set[str] = field(default_factory=set)
    _started_at: float = field(default_factory=time.monotonic)
    _closed: bool = False

    def _claim_name(self, name: str) -> str:
        """Register a docker resource name before the resource exists.

        Claiming up front means a container orphaned by a crash between
        `docker run` and bookkeeping is still shielded from the reaper until
        this sandbox closes, at which point it becomes reapable.
        """
        self.owned_names.add(name)
        _claim_sandbox_name(name)
        return name

    @classmethod
    def from_servers(
        cls,
        task_id: str,
        *,
        local_servers: Iterable[str],
        network_servers: Iterable[str],
    ) -> "TaskSandbox":
        return cls(
            task_id=task_id,
            local_servers=set(local_servers),
            network_servers=set(network_servers),
            agent_image=os.getenv(
                "MCP_TASK_AGENT_IMAGE", DEFAULT_RUNTIME_IMAGE
            ),
            mongo_image=(os.getenv("MCP_TASK_MONGO_IMAGE") or "").strip(),
            startup_timeout=float(
                os.getenv("MCP_TASK_SANDBOX_STARTUP_TIMEOUT", "180")
            ),
            memory_limit=os.getenv("MCP_TASK_SANDBOX_MEMORY", "3g"),
            cpu_limit=os.getenv("MCP_TASK_SANDBOX_CPUS", "2.0"),
            task_data_source=(os.getenv("MCP_TASK_DATA_DIR") or "").strip(),
        )

    @property
    def local_url(self) -> Optional[str]:
        return next(
            (container.url for container in self.containers if container.kind == "local"),
            None,
        )

    @property
    def local_container_name(self) -> Optional[str]:
        return next(
            (
                container.name
                for container in self.containers
                if container.kind == "local"
            ),
            None,
        )

    @property
    def network_url(self) -> Optional[str]:
        return next(
            (
                container.url
                for container in self.containers
                if container.kind == "network"
            ),
            None,
        )

    async def start(self) -> "TaskSandbox":
        write_runtime_event(
            "sandbox",
            "task_sandbox_starting",
            task_id=self.task_id,
            agent_image=self.agent_image,
            mongo_image=self.mongo_image if "mongodb" in self.local_servers else None,
            local_servers=sorted(self.local_servers),
            network_servers=sorted(self.network_servers),
        )
        try:
            if not self.task_data_source:
                raise TaskSandboxError(
                    "MCP_TASK_DATA_DIR is required for isolated tasks; "
                    "prepare an external task-data fixture first"
                )
            self.runtime_image = await inspect_runtime_image(self.agent_image)
            (
                self.task_data_dir,
                self.task_data_fixture,
                self.git_repositories,
            ) = await asyncio.to_thread(
                prepare_task_workspace,
                source_dir=self.task_data_source,
                task_id=_safe_fragment(self.task_id, 24),
                include_git="git" in self.local_servers,
            )
            self.task_workspace = self.task_data_dir.parent
            write_runtime_event(
                "sandbox",
                "task_data_injected",
                task_id=self.task_id,
                fixture_id=self.task_data_fixture.fixture_id,
                fixture_sha256=self.task_data_fixture.content_sha256,
                source_dir=str(self.task_data_fixture.source_dir),
                task_data_dir=str(self.task_data_dir),
                git_repositories=self.git_repositories,
                runtime_image_id=self.runtime_image["image_id"],
                runtime_version=self.runtime_image["runtime_version"],
            )
            if self.local_servers:
                await self._start_local_stack()
            if self.network_servers:
                await self._start_agent_container(
                    kind="network",
                    enabled_servers=self.network_servers,
                    network="bridge",
                    extra_env={},
                )
        except BaseException:
            await asyncio.shield(self.close())
            raise

        write_runtime_event(
            "sandbox",
            "task_sandbox_ready",
            task_id=self.task_id,
            local_url=self.local_url,
            network_url=self.network_url,
            duration_seconds=round(time.monotonic() - self._started_at, 3),
        )
        return self

    async def _start_local_stack(self) -> None:
        extra_env: dict[str, str] = {}
        if "mongodb" in self.local_servers:
            if not self.mongo_image:
                raise TaskSandboxError(
                    "MCP_TASK_MONGO_IMAGE is required for MongoDB tasks; "
                    "build a synthetic fixture image first"
                )
            self.mongo_fixture = await inspect_mongo_fixture_image(
                self.mongo_image
            )
            await self._create_mongo_socket_volume()
            await self._start_mongo()
            extra_env["MONGODB_CONNECTION_STRING"] = (
                "mongodb://%2Frun%2Fmcp-atlas-mongo%2Fmongodb-27017.sock"
            )

        await self._start_agent_container(
            kind="local",
            enabled_servers=self.local_servers,
            network="none",
            extra_env=extra_env,
        )

    async def _create_mongo_socket_volume(self) -> None:
        self.mongo_socket_volume = self._claim_name(
            f"mcp-atlas-mongo-socket-{_safe_fragment(self.task_id, 20)}-"
            f"{uuid.uuid4().hex[:10]}"
        )
        await _run(
            "docker",
            "volume",
            "create",
            "--label",
            "mcp-atlas.task-sandbox=true",
            "--label",
            f"mcp-atlas.owner={self.owner}",
            "--label",
            f"mcp-atlas.task-id={_safe_fragment(self.task_id, 50)}",
            self.mongo_socket_volume,
        )
        await _run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{self.mongo_socket_volume}:/run/mcp-atlas-mongo",
            "--entrypoint",
            "/bin/bash",
            self.mongo_image,
            "-lc",
            (
                "chown mongodb:mongodb /run/mcp-atlas-mongo "
                "&& chmod 0777 /run/mcp-atlas-mongo"
            ),
        )
        write_runtime_event(
            "sandbox",
            "task_mongo_socket_volume_created",
            task_id=self.task_id,
            volume=self.mongo_socket_volume,
        )

    async def _start_mongo(self) -> None:
        name = self._claim_name(
            f"mcp-atlas-mongo-{_safe_fragment(self.task_id, 20)}-"
            f"{uuid.uuid4().hex[:10]}"
        )
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--label",
            "mcp-atlas.task-sandbox=true",
            "--label",
            f"mcp-atlas.owner={self.owner}",
            "--label",
            f"mcp-atlas.task-id={_safe_fragment(self.task_id, 50)}",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "SETUID",
            "--pids-limit",
            "256",
            "--memory",
            os.getenv("MCP_TASK_MONGO_MEMORY", "1g"),
            "--cpus",
            os.getenv("MCP_TASK_MONGO_CPUS", "1.0"),
            "--tmpfs",
            "/data/db:rw,nosuid,nodev,size=768m",
            "--volume",
            f"{self.mongo_socket_volume}:/run/mcp-atlas-mongo",
            self.mongo_image,
            "mongod",
            "--bind_ip",
            "127.0.0.1",
            "--unixSocketPrefix",
            "/run/mcp-atlas-mongo",
            "--filePermissions",
            "0777",
        ]
        await _run(*command, timeout=120)
        container = ManagedContainer(kind="mongo", name=name, task_id=self.task_id)
        self.containers.append(container)
        write_runtime_event(
            "sandbox",
            "task_container_started",
            task_id=self.task_id,
            kind="mongo",
            container=name,
            image=self.mongo_image,
        )
        await self._wait_for_mongo(name)
        await self._restore_mongo_fixture(name)

    async def _restore_mongo_fixture(self, name: str) -> None:
        await _run(
            "docker",
            "exec",
            name,
            "mongorestore",
            "--drop",
            "--nsInclude=store.*",
            "/opt/mcp-task-fixture/dump",
            timeout=180,
        )
        write_runtime_event(
            "sandbox",
            "task_mongo_fixture_restored",
            task_id=self.task_id,
            container=name,
            image=self.mongo_image,
            database="store",
            image_id=(self.mongo_fixture or {}).get("image_id"),
            fixture_id=(self.mongo_fixture or {}).get("fixture_id"),
            fixture_sha256=(self.mongo_fixture or {}).get("content_sha256"),
        )

    async def _wait_for_mongo(self, name: str) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error = ""
        while time.monotonic() < deadline:
            stdout, stderr, code = await _run(
                "docker",
                "exec",
                name,
                "mongosh",
                "mongodb://127.0.0.1:27017",
                "--quiet",
                "--eval",
                "db.adminCommand({ping:1}).ok",
                timeout=15,
                check=False,
            )
            if code == 0 and stdout.strip().endswith("1"):
                write_runtime_event(
                    "sandbox",
                    "task_mongo_ready",
                    task_id=self.task_id,
                    container=name,
                )
                return
            state_out, _, _ = await _run(
                "docker",
                "inspect",
                name,
                "--format",
                "{{.State.Running}} {{.State.ExitCode}}",
                timeout=10,
                check=False,
            )
            if state_out.startswith("false "):
                raise TaskSandboxError(
                    f"Mongo fixture container {name} exited: {state_out}"
                )
            last_error = stderr or stdout
            await asyncio.sleep(1)
        raise TaskSandboxError(
            f"Mongo fixture container {name} was not ready: {last_error}"
        )

    async def _start_agent_container(
        self,
        *,
        kind: str,
        enabled_servers: Iterable[str],
        network: str,
        extra_env: dict[str, str],
    ) -> None:
        servers = tuple(sorted(set(enabled_servers)))
        name = self._claim_name(
            f"mcp-atlas-{kind}-{_safe_fragment(self.task_id, 20)}-"
            f"{uuid.uuid4().hex[:10]}"
        )
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--label",
            "mcp-atlas.task-sandbox=true",
            "--label",
            f"mcp-atlas.owner={self.owner}",
            "--label",
            f"mcp-atlas.task-id={_safe_fragment(self.task_id, 50)}",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "512",
            "--memory",
            self.memory_limit,
            "--cpus",
            self.cpu_limit,
            "--env",
            f"ENABLED_SERVERS={','.join(servers)}",
            "--volume",
            f"{self.task_data_dir}:/data:rw",
        ]
        if kind == "local":
            repository_root = Path(__file__).resolve().parents[3]
            local_template = (
                repository_root
                / "services"
                / "task-sandbox"
                / "local_mcp_server_template.json"
            )
            if not local_template.is_file():
                raise TaskSandboxError(
                    f"Missing task-local MCP template: {local_template}"
                )
            command.extend(
                [
                    "--volume",
                    (
                        f"{local_template}:"
                        "/agent-environment/src/agent_environment/"
                        "mcp_server_template.json:ro"
                    ),
                ]
            )
            if self.mongo_socket_volume:
                command.extend(
                    [
                        "--volume",
                        f"{self.mongo_socket_volume}:/run/mcp-atlas-mongo",
                    ]
                )
        if network == "bridge":
            command.extend(["--publish", "127.0.0.1::1984"])
        for key, value in sorted(extra_env.items()):
            command.extend(["--env", f"{key}={value}"])
        command.append(self.agent_image)
        if kind == "local":
            command.extend(
                [
                    "/agent-environment/.venv/bin/python",
                    "-m",
                    "uvicorn",
                    "agent_environment.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "1984",
                ]
            )
        else:
            command.extend(
                [
                    "/agent-environment/.venv/bin/python",
                    "-m",
                    "uvicorn",
                    "agent_environment.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "1984",
                ]
            )

        await _run(*command, timeout=120)
        container = ManagedContainer(
            kind=kind,
            name=name,
            task_id=self.task_id,
            enabled_servers=servers,
        )
        self.containers.append(container)
        write_runtime_event(
            "sandbox",
            "task_container_started",
            task_id=self.task_id,
            kind=kind,
            container=name,
            image=self.agent_image,
            image_id=(self.runtime_image or {}).get("image_id"),
            runtime_version=(self.runtime_image or {}).get("runtime_version"),
            fixture_id=(
                self.task_data_fixture.fixture_id
                if self.task_data_fixture else None
            ),
            fixture_sha256=(
                self.task_data_fixture.content_sha256
                if self.task_data_fixture else None
            ),
            enabled_servers=servers,
            credential_env_names=sorted(extra_env),
            network=network,
        )
        if kind == "local":
            container.url = "http://127.0.0.1:1984"
        else:
            container.url = await self._container_url(name, network)
        await self._wait_for_agent(container)

    async def _container_url(self, name: str, network: str) -> str:
        if network != "bridge":
            stdout, stderr, code = await _run(
                "docker",
                "inspect",
                name,
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                timeout=10,
                check=False,
            )
            address = stdout.strip()
            if code == 0 and address:
                return f"http://{address}:1984"
            raise TaskSandboxError(
                f"No task-internal address for {name}: {stderr or stdout}"
            )

        deadline = time.monotonic() + 30
        last = ""
        while time.monotonic() < deadline:
            stdout, stderr, code = await _run(
                "docker",
                "port",
                name,
                "1984/tcp",
                timeout=10,
                check=False,
            )
            if code == 0 and stdout:
                endpoint = stdout.splitlines()[0].strip()
                port = endpoint.rsplit(":", 1)[-1]
                if port.isdigit():
                    return f"http://127.0.0.1:{port}"
            last = stderr or stdout
            await asyncio.sleep(0.25)
        raise TaskSandboxError(f"No published port for {name}: {last}")

    async def _wait_for_agent(self, container: ManagedContainer) -> None:
        assert container.url
        deadline = time.monotonic() + self.startup_timeout
        last_error = ""
        async with httpx.AsyncClient(timeout=15) as client:
            while time.monotonic() < deadline:
                try:
                    if container.kind == "local":
                        status, body = await docker_post_json(
                            container.name,
                            "/list-tools",
                            {},
                            timeout=15,
                        )
                        tools = json.loads(body) if status == 200 else None
                    else:
                        response = await client.post(
                            f"{container.url}/list-tools"
                        )
                        status = response.status_code
                        body = response.text
                        tools = response.json() if status == 200 else None
                    if status == 200:
                        if tools:
                            write_runtime_event(
                                "sandbox",
                                "task_container_ready",
                                task_id=self.task_id,
                                kind=container.kind,
                                container=container.name,
                                tool_count=len(tools),
                                url=container.url,
                            )
                            return
                        last_error = "list-tools returned no tools"
                    else:
                        last_error = f"HTTP {status}: {body[:300]}"
                except Exception as exc:
                    last_error = str(exc)
                await asyncio.sleep(1)

        raise TaskSandboxError(
            f"Task container {container.name} was not ready: {last_error}"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._close_resources()
        finally:
            # Released last: anything this sandbox created but failed to remove
            # is now fair game for the periodic reaper.
            _release_sandbox_names(self.owned_names)

    async def _close_resources(self) -> None:
        for container in reversed(self.containers):
            await self._capture_logs(container)
            _, stderr, code = await _run(
                "docker",
                "rm",
                "-f",
                "-v",
                container.name,
                timeout=20,
                check=False,
            )
            write_runtime_event(
                "sandbox",
                "task_container_stopped",
                task_id=self.task_id,
                kind=container.kind,
                container=container.name,
                ok=code == 0,
                error=stderr if code else None,
            )
        self.containers.clear()

        if self.mongo_socket_volume:
            volume = self.mongo_socket_volume
            _, stderr, code = await _run(
                "docker",
                "volume",
                "rm",
                "-f",
                volume,
                timeout=20,
                check=False,
            )
            write_runtime_event(
                "sandbox",
                "task_mongo_socket_volume_removed",
                task_id=self.task_id,
                volume=volume,
                ok=code == 0,
                error=stderr if code else None,
            )
            self.mongo_socket_volume = None

        if self.task_workspace:
            workspace = self.task_workspace
            await asyncio.to_thread(shutil.rmtree, workspace, True)
            write_runtime_event(
                "sandbox",
                "task_data_removed",
                task_id=self.task_id,
                workspace=str(workspace),
            )
            self.task_workspace = None
            self.task_data_dir = None

        write_runtime_event(
            "sandbox",
            "task_sandbox_closed",
            task_id=self.task_id,
            duration_seconds=round(time.monotonic() - self._started_at, 3),
        )

    async def _capture_logs(self, container: ManagedContainer) -> None:
        stdout, stderr, _ = await _run(
            "docker",
            "logs",
            "--timestamps",
            container.name,
            timeout=30,
            check=False,
        )
        path = container_log_path(self.task_id, container.kind, container.name)
        text = stdout
        if stderr:
            text = f"{text}\n[stderr]\n{stderr}" if text else stderr
        try:
            path.write_text(text + ("\n" if text else ""), encoding="utf-8")
        except OSError as exc:
            write_runtime_event(
                "sandbox",
                "container_log_write_failed",
                task_id=self.task_id,
                container=container.name,
                error=str(exc),
            )


async def reap_owned_task_sandboxes() -> None:
    """Remove orphaned resources from a previous process using the same port."""

    owner = _owner_label()
    stdout, _, _ = await _run(
        "docker",
        "ps",
        "-aq",
        "--filter",
        "label=mcp-atlas.task-sandbox=true",
        "--filter",
        f"label=mcp-atlas.owner={owner}",
        timeout=30,
        check=False,
    )
    containers = [line.strip() for line in stdout.splitlines() if line.strip()]
    for container_id in containers:
        await _run(
            "docker",
            "rm",
            "-f",
            "-v",
            container_id,
            timeout=30,
            check=False,
        )

    volumes_out, _, _ = await _run(
        "docker",
        "volume",
        "ls",
        "-q",
        "--filter",
        "label=mcp-atlas.task-sandbox=true",
        "--filter",
        f"label=mcp-atlas.owner={owner}",
        timeout=30,
        check=False,
    )
    volumes = [line.strip() for line in volumes_out.splitlines() if line.strip()]
    for volume in volumes:
        await _run(
            "docker",
            "volume",
            "rm",
            "-f",
            volume,
            timeout=30,
            check=False,
        )

    write_runtime_event(
        "sandbox",
        "orphan_reap_completed",
        owner=owner,
        containers_removed=len(containers),
        volumes_removed=len(volumes),
    )


async def _stale_named_resources(
    list_args: tuple[str, ...],
    inspect_args: tuple[str, ...],
    *,
    min_age_seconds: float,
    now: Optional[float] = None,
) -> list[str]:
    """Return owned-label resources that are untracked and old enough to reap.

    Two independent guards must both agree before a name is returned: it is
    absent from the in-process registry, and docker reports it as older than
    `min_age_seconds`. A resource whose creation time cannot be read is left
    alone.
    """
    stdout, _, _ = await _run(*list_args, timeout=30, check=False)
    names = [line.strip() for line in stdout.splitlines() if line.strip()]
    candidates = [name for name in names if name not in _LIVE_SANDBOX_NAMES]
    if not candidates:
        return []

    stdout, _, code = await _run(
        *inspect_args, *candidates, timeout=30, check=False
    )
    if code != 0:
        return []

    cutoff = (time.time() if now is None else now) - min_age_seconds
    stale: list[str] = []
    for line in stdout.splitlines():
        name, _, created = line.strip().partition("\t")
        name = name.lstrip("/")
        if not name or name in _LIVE_SANDBOX_NAMES:
            continue
        created_at = _parse_docker_timestamp(created)
        if created_at is not None and created_at <= cutoff:
            stale.append(name)
    return stale


async def reap_orphan_task_sandboxes(min_age_seconds: float) -> dict[str, int]:
    """Reclaim sandbox resources this process created but no longer tracks.

    Covers the leaks that startup reaping cannot: a sandbox whose teardown was
    interrupted mid-run leaves containers holding memory and CPU for as long as
    the service stays up.
    """
    owner = _owner_label()
    label_filters = (
        "--filter",
        "label=mcp-atlas.task-sandbox=true",
        "--filter",
        f"label=mcp-atlas.owner={owner}",
    )

    containers = await _stale_named_resources(
        ("docker", "ps", "-a", "--format", "{{.Names}}", *label_filters),
        ("docker", "inspect", "--format", "{{.Name}}\t{{.Created}}"),
        min_age_seconds=min_age_seconds,
    )
    for name in containers:
        await _run("docker", "rm", "-f", "-v", name, timeout=30, check=False)

    volumes = await _stale_named_resources(
        ("docker", "volume", "ls", "-q", *label_filters),
        ("docker", "volume", "inspect", "--format", "{{.Name}}\t{{.CreatedAt}}"),
        min_age_seconds=min_age_seconds,
    )
    for name in volumes:
        await _run("docker", "volume", "rm", "-f", name, timeout=30, check=False)

    if containers or volumes:
        write_runtime_event(
            "sandbox",
            "orphan_sweep_reclaimed",
            owner=owner,
            min_age_seconds=min_age_seconds,
            containers=containers,
            volumes=volumes,
        )
    return {"containers": len(containers), "volumes": len(volumes)}


async def run_orphan_sweeper(
    *, interval_seconds: float, min_age_seconds: float
) -> None:
    """Sweep for orphaned sandbox resources until cancelled."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reap_orphan_task_sandboxes(min_age_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # sweeping must never kill the service
            write_runtime_event(
                "sandbox",
                "orphan_sweep_failed",
                error=str(exc),
            )
