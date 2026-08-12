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


def _teardown_timeout() -> float:
    """Seconds to wait on a single teardown docker command.

    20s was too tight: with 30 tasks concurrently creating and removing
    containers, the docker daemon queues and `docker rm` overruns it.
    """
    return float(os.getenv("MCP_SANDBOX_TEARDOWN_TIMEOUT", "90"))


def _reap_concurrency() -> int:
    value = int(os.getenv("MCP_SANDBOX_REAP_CONCURRENCY", "4"))
    if value < 1:
        raise ValueError("MCP_SANDBOX_REAP_CONCURRENCY must be positive")
    return value


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
    task_network: Optional[str] = None
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
            if self.local_servers or self.network_servers:
                await self._create_task_network()
            if not self.task_network:
                raise TaskSandboxError("task network was not created")
            if self.local_servers:
                await self._start_local_stack()
            if self.network_servers:
                await self._start_agent_container(
                    kind="network",
                    enabled_servers=self.network_servers,
                    network=self.task_network,
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

    async def _create_task_network(self) -> None:
        name = self._claim_name(
            f"mcp-atlas-net-{_safe_fragment(self.task_id, 20)}-"
            f"{uuid.uuid4().hex[:10]}"
        )
        await _run(
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--opt",
            "com.docker.network.bridge.enable_icc=false",
            "--label",
            "mcp-atlas.task-sandbox=true",
            "--label",
            f"mcp-atlas.owner={self.owner}",
            "--label",
            f"mcp-atlas.task-id={_safe_fragment(self.task_id, 50)}",
            name,
        )
        self.task_network = name
        write_runtime_event(
            "sandbox",
            "task_network_created",
            task_id=self.task_id,
            network=name,
            driver="bridge",
            inter_container_communication=False,
        )

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
            network=self.task_network or "",
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
        # task-network services are reached over a loopback-published random
        # port.  task-local services use docker-exec HTTP, so they need outbound
        # networking but must not publish a host port.
        if kind == "network":
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
            container.url = await self._container_url(name)
        await self._wait_for_agent(container)

    async def _container_url(self, name: str) -> str:
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Teardown runs after the task's answer is already complete. A
            # docker hiccup here must never be reported as task failure, or the
            # caller reruns a finished task and pays for the tokens again.
            write_runtime_event(
                "sandbox",
                "task_sandbox_close_failed",
                task_id=self.task_id,
                error=str(exc),
            )
        finally:
            # Released last: anything this sandbox created but failed to remove
            # is now fair game for the periodic reaper.
            _release_sandbox_names(self.owned_names)

    async def _remove_resource(self, *args: str, event: str, **fields) -> None:
        """Remove one docker resource, absorbing any failure.

        Under load `docker rm` can exceed its timeout, which _run raises on.
        Whatever survives is left to the orphan sweeper rather than aborting
        the rest of the teardown.
        """
        try:
            _, stderr, code = await _run(
                *args, timeout=_teardown_timeout(), check=False
            )
            ok, error = code == 0, (stderr if code else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ok, error = False, str(exc)

        write_runtime_event(
            "sandbox", event, task_id=self.task_id, ok=ok, error=error, **fields
        )

    async def _close_resources(self) -> None:
        for container in reversed(self.containers):
            try:
                await self._capture_logs(container)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                write_runtime_event(
                    "sandbox",
                    "container_log_capture_failed",
                    task_id=self.task_id,
                    container=container.name,
                    error=str(exc),
                )
            await self._remove_resource(
                "docker",
                "rm",
                "-f",
                "-v",
                container.name,
                event="task_container_stopped",
                kind=container.kind,
                container=container.name,
            )
        self.containers.clear()

        if self.mongo_socket_volume:
            volume = self.mongo_socket_volume
            await self._remove_resource(
                "docker",
                "volume",
                "rm",
                "-f",
                volume,
                event="task_mongo_socket_volume_removed",
                volume=volume,
            )
            self.mongo_socket_volume = None

        if self.task_network:
            network = self.task_network
            await self._remove_resource(
                "docker",
                "network",
                "rm",
                network,
                event="task_network_removed",
                network=network,
            )
            self.task_network = None

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


async def _reap_one(*args: str) -> bool:
    """Run one reaping command, absorbing timeouts and docker errors."""
    try:
        _, _, code = await _run(
            *args, timeout=_teardown_timeout(), check=False
        )
        return code == 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        write_runtime_event(
            "sandbox",
            "orphan_reap_step_failed",
            command=" ".join(args[:3]),
            target=args[-1],
            error=str(exc),
        )
        return False


async def reap_owned_task_sandboxes() -> dict[str, int]:
    """Best-effort cleanup restricted to this completion service's labels.

    Deletions use bounded concurrency so a backlog does not turn into N serial
    teardown timeouts on a busy shared Docker daemon. Callers receive verified
    remaining counts and can decide whether an incomplete cleanup is fatal.
    """

    owner = _owner_label()
    label_filters = (
        "--filter",
        "label=mcp-atlas.task-sandbox=true",
        "--filter",
        f"label=mcp-atlas.owner={owner}",
    )

    async def list_owned(kind: str) -> tuple[list[str], bool]:
        if kind == "container":
            command = ("docker", "ps", "-aq", *label_filters)
        elif kind == "volume":
            command = ("docker", "volume", "ls", "-q", *label_filters)
        else:
            command = ("docker", "network", "ls", "-q", *label_filters)
        try:
            stdout, _, _ = await _run(
                *command, timeout=_teardown_timeout(), check=False
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            write_runtime_event(
                "sandbox",
                "orphan_reap_listing_failed",
                kind=kind,
                error=str(exc),
            )
            return [], True
        return (
            [line.strip() for line in stdout.splitlines() if line.strip()],
            False,
        )

    semaphore = asyncio.Semaphore(_reap_concurrency())

    async def remove_one(kind: str, name: str) -> bool:
        if kind == "container":
            command = ("docker", "rm", "-f", "-v", name)
        elif kind == "volume":
            command = ("docker", "volume", "rm", "-f", name)
        else:
            command = ("docker", "network", "rm", name)
        async with semaphore:
            return await _reap_one(*command)

    async def remove_owned(kind: str) -> tuple[int, int, int, int]:
        initial, initial_listing_failed = await list_owned(kind)
        results = await asyncio.gather(
            *(remove_one(kind, name) for name in initial)
        ) if initial else []
        remaining, final_listing_failed = await list_owned(kind)
        failed_names = {
            name for name, ok in zip(initial, results) if not ok
        }
        if final_listing_failed:
            # A failed verification cannot be reported as a clean result.
            remaining_count = len(failed_names)
            failures = len(failed_names)
            removed_count = len(initial) - remaining_count
        else:
            remaining_count = len(remaining)
            remaining_names = set(remaining)
            # A Docker CLI timeout can still finish inside the daemon. Count it
            # as a failure only when verification says the target survived.
            failures = len(failed_names & remaining_names)
            removed_count = len(initial) - len(set(initial) & remaining_names)
        return (
            max(0, removed_count),
            remaining_count,
            failures,
            int(initial_listing_failed) + int(final_listing_failed),
        )

    (
        containers_removed,
        containers_remaining,
        container_failures,
        container_listing_failures,
    ) = await remove_owned("container")
    (
        volumes_removed,
        volumes_remaining,
        volume_failures,
        volume_listing_failures,
    ) = await remove_owned("volume")
    (
        networks_removed,
        networks_remaining,
        network_failures,
        network_listing_failures,
    ) = await remove_owned("network")
    listing_failures = (
        container_listing_failures
        + volume_listing_failures
        + network_listing_failures
    )
    result = {
        "containers_removed": containers_removed,
        "containers_remaining": containers_remaining,
        "volumes_removed": volumes_removed,
        "volumes_remaining": volumes_remaining,
        "networks_removed": networks_removed,
        "networks_remaining": networks_remaining,
        "removal_failures": (
            container_failures + volume_failures + network_failures
        ),
        "listing_failures": listing_failures,
    }

    write_runtime_event(
        "sandbox",
        "orphan_reap_completed",
        owner=owner,
        **result,
    )
    return result


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

    networks = await _stale_named_resources(
        ("docker", "network", "ls", "-q", *label_filters),
        (
            "docker",
            "network",
            "inspect",
            "--format",
            "{{.Name}}\t{{.Created}}",
        ),
        min_age_seconds=min_age_seconds,
    )
    for name in networks:
        await _run("docker", "network", "rm", name, timeout=30, check=False)

    if containers or volumes or networks:
        write_runtime_event(
            "sandbox",
            "orphan_sweep_reclaimed",
            owner=owner,
            min_age_seconds=min_age_seconds,
            containers=containers,
            volumes=volumes,
            networks=networks,
        )
    return {
        "containers": len(containers),
        "volumes": len(volumes),
        "networks": len(networks),
    }


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
