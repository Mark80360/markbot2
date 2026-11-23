"""MCP 客户端扩展。

对 MCP（Model Context Protocol）的接入遵循以下约定，与 nanobot 的实现保持一致：

- 每个 MCP 工具映射为 ``mcp_<server>_<tool>``，名称进入模型前做字符清洗与 64 字符截断；
- ``enabled_tools`` 是工具白名单：``["*"]`` 表示注册全部能力（工具/资源/提示），
  指定名单时只注册列出的工具，且不注册资源与提示；
- 工具入参使用服务端下发的 JSON Schema，客户端只做 provider 兼容化处理
  （本地 ``$ref`` 提升到 ``$defs``、``nullable`` 归一化），不改变服务端校验语义；
- 只读/幂等的工具在临时故障后可安全重试一次；有副作用的调用一旦发出就不再重放，
  连接中断时返回"结果未知"，避免重复副作用；
- 日志中的服务端 URL 去掉凭据与查询串；
- 连接失败只影响该 server，其它 server 与进程继续工作。
- ``capabilities`` 给该 server 的全部工具声明能力（如 ``["net:*", "ui"]``），走统一的
  权限策略：strict 档位下声明了 ``net:`` 的工具会被拒绝；``confirm_tools`` 是需要逐次
  用户确认的工具名通配列表（如浏览器的点击、输入、执行脚本），无人值守时一律拒绝（ADR 0009）。

MCP 连接绑定事件循环生命周期。markbot 的回合跑在各自的 ``asyncio.run`` 里，
因此本扩展独占一个后台事件循环线程，跨回合复用连接，并在 shutdown 时关闭。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from markbot.api import EventName, Tool, ToolResult

logger = logging.getLogger("markbot.mcp")

_MAX_TOOL_NAME_LENGTH = 64
_HASH_LENGTH = 8
_HTTP_READ_TIMEOUT = 300.0
_SANITIZE_RE = re.compile(r"_+")
_TRANSIENT_EXC_NAMES = {
    "AnyIOError",
    "BrokenResourceError",
    "ClosedResourceError",
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "EndOfStream",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutError",
}


@dataclass
class _ServerSpec:
    name: str
    type: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    tool_timeout: float = 30.0
    connect_timeout: float = 20.0
    enabled_tools: list[str] = field(default_factory=lambda: ["*"])
    # 该 server 全部工具的能力声明（走 PermissionPolicy）；需要逐次确认的工具名通配
    capabilities: list[str] = field(default_factory=list)
    confirm_tools: list[str] = field(default_factory=list)

    @property
    def transport(self) -> str:
        if self.type:
            return self.type
        if self.command:
            return "stdio"
        if self.url:
            return "sse" if self.url.rstrip("/").endswith("/sse") else "streamableHttp"
        return ""

    def signature(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "env": sorted(self.env.items()),
            "cwd": self.cwd,
            "url": self.url,
            "headers": sorted(self.headers.items()),
            "tool_timeout": self.tool_timeout,
            "connect_timeout": self.connect_timeout,
            "enabled_tools": list(self.enabled_tools),
            "capabilities": list(self.capabilities),
            "confirm_tools": list(self.confirm_tools),
        }


class _RawArgs(BaseModel):
    """动态工具的占位参数模型；真实契约由服务端 JSON Schema 描述。"""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- 名称与 schema


def _sanitize_name(name: str) -> str:
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _limit_tool_name(name: str, max_length: int = _MAX_TOOL_NAME_LENGTH) -> str:
    if len(name) <= max_length:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    prefix_length = max_length - _HASH_LENGTH - 1
    return f"{name[:prefix_length]}_{digest}"


def _sanitize_mcp_tool_name(name: str) -> str:
    return _limit_tool_name(_sanitize_name(name))


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    if not isinstance(options, list):
        return None
    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)
    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _resolve_local_schema_ref(root: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#"):
        raise ValueError("not a local JSON Pointer")
    pointer = urllib.parse.unquote(ref[1:], errors="strict")
    if not pointer:
        return root
    if not pointer.startswith("/"):
        raise ValueError("not a local JSON Pointer")
    current: Any = root
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise KeyError(part)
    return current


def _rewrite_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把任意本地 ``$ref`` 提升为 provider 兼容的 ``#/$defs/<name>``。"""
    rewritten_refs: dict[str, str] = {}
    generated_defs: dict[str, Any] = {}

    def rewrite(value: Any) -> Any:
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value

        rewritten = dict(value)
        raw_ref = rewritten.get("$ref")
        ref = raw_ref if isinstance(raw_ref, str) else None
        is_rewritable_ref = False
        if ref is not None and not ref.startswith("#/$defs/"):
            try:
                pointer = urllib.parse.unquote(ref[1:], errors="strict")
            except (UnicodeDecodeError, ValueError):
                pass
            else:
                is_rewritable_ref = ref.startswith("#") and (
                    not pointer or pointer.startswith("/")
                )
        if is_rewritable_ref:
            assert ref is not None
            name = rewritten_refs.get(ref)
            if name is None:
                try:
                    target = _resolve_local_schema_ref(schema, ref)
                except (KeyError, IndexError, TypeError, UnicodeDecodeError, ValueError):
                    logger.warning("MCP 工具 schema 含无法解析的本地 $ref: %s", ref)
                else:
                    name = f"ref_{hashlib.sha256(ref.encode()).hexdigest()[:12]}"
                    existing_defs = schema.get("$defs")
                    while isinstance(existing_defs, dict) and name in existing_defs:
                        name += "_"
                    rewritten_refs[ref] = name
                    # 先占位再递归，循环引用才能终止
                    generated_defs[name] = {}
                    generated_defs[name] = rewrite(target)
            if name is not None:
                rewritten["$ref"] = f"#/$defs/{name}"

        return {key: rewrite(item) for key, item in rewritten.items()}

    result = rewrite(schema)
    if generated_defs:
        existing_defs = result.get("$defs")
        result["$defs"] = {
            **(existing_defs if isinstance(existing_defs, dict) else {}),
            **generated_defs,
        }
    return result


def _normalize_nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(schema)
    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["properties"] = {
            name: _normalize_nullable_schema(prop) if isinstance(prop, dict) else prop
            for name, prop in properties.items()
        }
    items = normalized.get("items")
    if isinstance(items, dict):
        normalized["items"] = _normalize_nullable_schema(items)
    definitions = normalized.get("$defs")
    if isinstance(definitions, dict):
        normalized["$defs"] = {
            name: _normalize_nullable_schema(definition)
            if isinstance(definition, dict)
            else definition
            for name, definition in definitions.items()
        }

    if normalized.get("type") == "object":
        normalized.setdefault("properties", {})
        normalized.setdefault("required", [])
    return normalized


def _normalize_schema_for_provider(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    return _normalize_nullable_schema(_rewrite_local_schema_refs(schema))


# ---------------------------------------------------------------- 配置解析


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, value)


def _pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _parse_servers(config: Mapping[str, Any]) -> dict[str, _ServerSpec]:
    tools = config.get("tools") if isinstance(config.get("tools"), Mapping) else {}
    raw_servers = (
        _pick(tools, "mcp_servers", "mcpServers")
        or _pick(config, "mcp_servers", "mcpServers")
        or {}
    )
    if not isinstance(raw_servers, Mapping):
        return {}

    servers: dict[str, _ServerSpec] = {}
    for name, raw in raw_servers.items():
        if not isinstance(raw, Mapping):
            logger.warning("MCP server '%s': 配置不是表结构，已跳过", name)
            continue
        spec = _ServerSpec(name=str(name))
        spec.type = str(_pick(raw, "type") or "").strip()
        spec.command = str(_pick(raw, "command") or "").strip()
        spec.args = [str(item) for item in (_pick(raw, "args") or [])]
        spec.env = {
            str(key): _expand_env(str(value))
            for key, value in (_pick(raw, "env") or {}).items()
        }
        spec.cwd = str(_pick(raw, "cwd") or "").strip()
        spec.url = str(_pick(raw, "url") or "").strip()
        spec.headers = {
            str(key): _expand_env(str(value))
            for key, value in (_pick(raw, "headers") or {}).items()
        }
        timeout = _pick(raw, "tool_timeout", "toolTimeout")
        spec.tool_timeout = float(timeout) if timeout is not None else 30.0
        connect_timeout = _pick(raw, "connect_timeout", "connectTimeout")
        spec.connect_timeout = float(connect_timeout) if connect_timeout is not None else 20.0
        enabled = _pick(raw, "enabled_tools", "enabledTools")
        spec.enabled_tools = (
            [str(item) for item in enabled] if isinstance(enabled, list) else ["*"]
        )
        caps = _pick(raw, "capabilities")
        spec.capabilities = _valid_capabilities(name, caps)
        confirm = _pick(raw, "confirm_tools", "confirmTools")
        if isinstance(confirm, str):
            confirm = [confirm]
        spec.confirm_tools = (
            [str(item).strip() for item in confirm if str(item).strip()]
            if isinstance(confirm, list)
            else []
        )

        if not spec.transport:
            logger.warning("MCP server '%s': 缺少 command 或 url，已跳过", name)
            continue
        if spec.transport in {"sse", "streamableHttp"}:
            if urllib.parse.urlsplit(spec.url).scheme not in {"http", "https"}:
                logger.warning("MCP server '%s': url 必须是 http(s)，已跳过", name)
                continue
        elif spec.transport == "stdio":
            if not spec.command:
                logger.warning("MCP server '%s': stdio 传输缺少 command，已跳过", name)
                continue
        else:
            logger.warning("MCP server '%s': 未知传输类型 %r，已跳过", name, spec.type)
            continue
        servers[spec.name] = spec
    return servers


_CAP_KINDS = ("fs.read:", "fs.write:", "net:", "exec:", "secret:", "browser:", "desktop:")


def _valid_capabilities(server: Any, raw: Any) -> list[str]:
    """只保留语法正确的能力声明；扩展不依赖 kernel，这里只做形状检查，语义由策略判定。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text == "ui" or (text.startswith(_CAP_KINDS) and text.split(":", 1)[1].strip()):
            out.append(text)
        elif text:
            logger.warning("MCP server '%s': 忽略无法识别的能力声明 %r", server, text)
    return out


def _needs_confirm(spec: _ServerSpec, raw_name: str) -> bool:
    return any(fnmatch.fnmatchcase(raw_name, pattern) for pattern in spec.confirm_tools)


async def _confirm_call(ctx: Any, server: str, raw_name: str, raw_args: Mapping[str, Any]) -> bool:
    confirm = getattr(ctx, "confirm", None)
    if not callable(confirm):
        return False
    preview = json.dumps(dict(raw_args), ensure_ascii=False)
    if len(preview) > 800:
        preview = f"{preview[:600]}\n…（共 {len(preview)} 字符）…\n{preview[-200:]}"
    return bool(await confirm(f"确认执行 MCP 工具 {server}/{raw_name}", preview))


def _signature(servers: Mapping[str, _ServerSpec]) -> str:
    payload = json.dumps(
        [servers[name].signature() for name in sorted(servers)],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_url(url: str) -> str:
    """日志里只保留协议、主机与端口，去掉凭据、路径细节与查询串。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    netloc = parts.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    path = "/..." if parts.path and parts.path != "/" else parts.path
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


# ---------------------------------------------------------------- 错误判定


def _is_transient(exc: BaseException) -> bool:
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def _is_session_terminated(exc: BaseException) -> bool:
    if _is_transient(exc):
        return True
    messages = [str(exc)]
    error = getattr(exc, "error", None)
    if error is not None:
        messages.append(str(getattr(error, "message", "")))
    return any(
        marker in message.lower()
        for marker in ("session terminated", "connection closed", "closed resource")
        for message in messages
    )


# ---------------------------------------------------------------- 私有事件循环


class _MCPLoop:
    """独占的事件循环线程：MCP 连接跨回合存活，且不阻塞内核循环。"""

    def __init__(self, name: str = "markbot-mcp") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._ready = threading.Event()
        self._closed = False

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            with contextlib.suppress(BaseException):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]

    def call(self, coro: Awaitable[Any], timeout: float) -> Any:
        return self.submit(coro).result(timeout)

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)


# ---------------------------------------------------------------- 连接


class _ServerRuntime:
    """一个 MCP server 的连接、重连与调用；所有协程跑在私有事件循环里。

    连接上下文由独立的 owner 任务持有：anyio 的 cancel scope 要求 enter/exit
    发生在同一个任务里，所以重连与关闭都通过让 owner 自己退出上下文完成。
    """

    def __init__(self, spec: _ServerSpec, loop: _MCPLoop) -> None:
        self.spec = spec
        self.loop = loop
        self.session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lifecycle: asyncio.Lock | None = None
        self.tools: list[Any] = []
        self.resources: list[Any] = []
        self.prompts: list[Any] = []
        self.replay_safe: dict[str, bool] = {}
        self.connected = False

    # -- 生命周期（均在私有循环中执行）
    async def start(self) -> bool:
        self._lifecycle = asyncio.Lock()
        return await self._start_owner()

    async def _start_owner(self) -> bool:
        if self.session is not None:
            return True
        ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._own(self._stop, ready), name=f"mcp:{self.spec.name}"
        )
        try:
            return await asyncio.wait_for(ready, self.spec.connect_timeout + 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._stop_owner()
            return False

    async def _own(self, stop: asyncio.Event, ready: asyncio.Future[bool]) -> None:
        stack = contextlib.AsyncExitStack()
        try:
            await stack.__aenter__()
            session = await self._open(stack)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await stack.aclose()
            if not ready.done():
                ready.set_result(False)
            raise
        except BaseException as exc:  # noqa: BLE001 - 单个 server 失败不影响其它
            with contextlib.suppress(BaseException):
                await stack.aclose()
            if not ready.done():
                ready.set_result(False)
            logger.warning(
                "MCP server '%s' 连接失败: %s%s",
                self.spec.name,
                type(exc).__name__,
                f" @ {_redact_url(self.spec.url)}" if self.spec.url else "",
            )
            logger.debug("MCP server '%s' 连接异常详情", self.spec.name, exc_info=exc)
            return

        self.session, self.connected = session, True
        if not ready.done():
            ready.set_result(True)
        logger.info(
            "MCP server '%s' 已连接（tools=%d, resources=%d, prompts=%d）",
            self.spec.name,
            len(self.tools),
            len(self.resources),
            len(self.prompts),
        )
        try:
            await stop.wait()
        finally:
            self.session, self.connected = None, False
            with contextlib.suppress(BaseException):
                await stack.aclose()

    async def _stop_owner(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), 5.0)
            except BaseException:  # noqa: BLE001 - 关闭失败不应阻断后续
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self.session, self.connected = None, False

    async def _open(self, stack: contextlib.AsyncExitStack) -> Any:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        transport = self.spec.transport
        if transport == "stdio":
            params = StdioServerParameters(
                command=self.spec.command,
                args=self.spec.args,
                env=self.spec.env or None,
                cwd=self.spec.cwd or None,
            )
            read, write = await asyncio.wait_for(
                stack.enter_async_context(stdio_client(params)),
                self.spec.connect_timeout,
            )
        elif transport == "sse":
            read, write = await asyncio.wait_for(
                stack.enter_async_context(
                    sse_client(self.spec.url, headers=self.spec.headers or None)
                ),
                self.spec.connect_timeout,
            )
        else:
            import httpx

            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=self.spec.headers or None,
                    follow_redirects=True,
                    timeout=httpx.Timeout(
                        _HTTP_READ_TIMEOUT,
                        connect=min(10.0, self.spec.connect_timeout),
                        read=max(_HTTP_READ_TIMEOUT, self.spec.tool_timeout),
                    ),
                )
            )
            streams = await asyncio.wait_for(
                stack.enter_async_context(
                    streamable_http_client(self.spec.url, http_client=client)
                ),
                self.spec.connect_timeout,
            )
            read, write = streams[0], streams[1]

        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), self.spec.connect_timeout)
        await self._discover(session)
        return session

    async def _discover(self, session: Any) -> None:
        from mcp import types

        self.tools = await asyncio.wait_for(self._list_tools(session), self.spec.connect_timeout)
        self.replay_safe = {
            str(getattr(item, "name", "")): _replay_safe(item) for item in self.tools
        }
        if "*" not in self.spec.enabled_tools:
            # enabledTools 是工具白名单；有约束时不注册资源与提示
            self.resources, self.prompts = [], []
            return
        self.resources = await self._list_pages(session.list_resources, "resources", types)
        self.prompts = await self._list_pages(session.list_prompts, "prompts", types)

    async def _list_tools(self, session: Any) -> list[Any]:
        from mcp import types

        return await self._list_pages(session.list_tools, "tools", types)

    @staticmethod
    async def _list_pages(
        list_fn: Callable[..., Awaitable[Any]], key: str, types: Any
    ) -> list[Any]:
        try:
            page = await list_fn()
        except Exception as exc:  # noqa: BLE001 - 能力缺失不是错误
            logger.debug("MCP 能力 %s 不可用: %s", key, type(exc).__name__)
            return []
        items = list(getattr(page, key, []) or [])
        seen_cursors: set[str] = set()
        paginated = getattr(types, "PaginatedRequestParams", None)
        while getattr(page, "nextCursor", None) is not None:
            cursor = page.nextCursor
            if cursor in seen_cursors:
                logger.warning("MCP %s/list 返回重复分页游标，已停止翻页", key)
                break
            seen_cursors.add(cursor)
            params = paginated(cursor=cursor) if paginated is not None else None
            page = await (list_fn(params=params) if params is not None else list_fn())
            items.extend(getattr(page, key, []) or [])
        return items

    async def aclose(self) -> None:
        if self._lifecycle is None:
            return
        async with self._lifecycle:
            await self._stop_owner()

    async def reset(self) -> bool:
        if self._lifecycle is None:
            self._lifecycle = asyncio.Lock()
        async with self._lifecycle:
            await self._stop_owner()
            return await self._start_owner()

    # -- 调用
    async def call(
        self,
        request: Callable[[Any], Awaitable[Any]],
        *,
        kind: str,
        label: str,
        replay_safe: bool,
        timeout: float,
    ) -> Any | ToolResult:
        future = self.loop.submit(
            self._request(
                request,
                kind=kind,
                label=label,
                replay_safe=replay_safe,
                timeout=timeout,
            )
        )
        return await asyncio.wrap_future(future)

    async def _request(
        self,
        request: Callable[[Any], Awaitable[Any]],
        *,
        kind: str,
        label: str,
        replay_safe: bool,
        timeout: float,
    ) -> Any | ToolResult:
        from mcp.shared.exceptions import McpError

        refreshed = False
        retried = False
        while True:
            if self.session is None and not await self.reset():
                return ToolResult.error(f"(MCP {kind} '{label}' 未连接：server 不可用)")
            session = self.session
            attempt_started = False
            try:
                if not replay_safe:
                    ping = getattr(session, "send_ping", None)
                    if ping is not None:
                        # 先探活：ping 失败可证明本次调用尚未发出
                        await asyncio.wait_for(ping(), timeout=timeout)
                attempt_started = True
                return await asyncio.wait_for(request(session), timeout=timeout)
            except asyncio.TimeoutError:
                if attempt_started and not replay_safe:
                    return self._unknown(label, f"超时 {timeout}s 后结果未知")
                logger.warning("MCP %s '%s' 调用超时（%ss）", kind, label, timeout)
                return ToolResult.error(f"(MCP {kind} '{label}' 调用超时 {timeout}s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 转成工具错误返回模型
                if attempt_started and not replay_safe and _is_session_terminated(exc):
                    return self._unknown(label, f"响应丢失（{type(exc).__name__}）")
                if not refreshed and _is_session_terminated(exc):
                    logger.warning(
                        "MCP server '%s' 会话中断，正在重连（%s）",
                        self.spec.name,
                        type(exc).__name__,
                    )
                    if await self.reset():
                        refreshed = True
                        continue
                if kind == "prompt" and isinstance(exc, McpError):
                    error = getattr(exc, "error", None)
                    return ToolResult.error(
                        f"(MCP prompt '{label}' 失败: {getattr(error, 'message', exc)})"
                    )
                if _is_transient(exc) and not retried:
                    logger.warning(
                        "MCP %s '%s' 遇到临时错误（%s），重试一次",
                        kind,
                        label,
                        type(exc).__name__,
                    )
                    retried = True
                    await asyncio.sleep(1)
                    continue
                logger.warning("MCP %s '%s' 调用失败: %s", kind, label, type(exc).__name__)
                logger.debug("MCP %s '%s' 失败详情", kind, label, exc_info=exc)
                return ToolResult.error(f"(MCP {kind} '{label}' 失败: {type(exc).__name__})")

    @staticmethod
    def _unknown(label: str, reason: str) -> ToolResult:
        logger.warning("MCP 调用 '%s' %s；结果未知，不重放", label, reason)
        return ToolResult.error(
            f"(MCP 工具调用结果未知：{reason}。该操作可能已执行；为避免重复副作用未自动重放，"
            "请先确认结果再决定是否重试。)"
        )


def _replay_safe(tool_def: Any) -> bool:
    annotations = getattr(tool_def, "annotations", None)
    return annotations is not None and (
        getattr(annotations, "readOnlyHint", None) is True
        or getattr(annotations, "idempotentHint", None) is True
    )


# ---------------------------------------------------------------- 结果渲染


def _render_blocks(blocks: Any) -> str:
    from mcp import types

    parts: list[str] = []
    for block in blocks or []:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.ImageContent):
            parts.append(
                f"[MCP 返回图片 {getattr(block, 'mimeType', '') or 'image/png'}，"
                f"base64 长度 {len(getattr(block, 'data', '') or '')}，未嵌入上下文]"
            )
        elif isinstance(block, types.EmbeddedResource):
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            if text is not None:
                parts.append(text)
            else:
                mime = getattr(resource, "mimeType", "") or "未知类型"
                parts.append(f"[MCP 返回二进制资源 {mime}]")
        else:
            parts.append(str(block))
    return "\n".join(part for part in parts if part) or "(无输出)"


def _render_prompt(result: Any) -> str:
    lines: list[str] = []
    for message in getattr(result, "messages", []) or []:
        role = getattr(message, "role", "user")
        lines.append(f"[{role}]\n{_render_blocks([getattr(message, 'content', None)])}")
    text = getattr(result, "description", None)
    if text:
        lines.insert(0, str(text))
    return "\n".join(line for line in lines if line) or "(无输出)"


# ---------------------------------------------------------------- 工具包装


def _tool_timeout_budget(timeout: float) -> float:
    return max(timeout + 5.0, 5.0)


def _make_tool(
    server: _ServerRuntime,
    tool_def: Any,
    raw_name: str,
) -> Tool | None:
    wrapped = _sanitize_mcp_tool_name(f"mcp_{server.spec.name}_{raw_name}")
    replay_safe = server.replay_safe.get(raw_name, False)
    timeout = server.spec.tool_timeout

    async def handler(ctx: Any, args: Any, _raw: str = raw_name) -> ToolResult:
        raw_args = dict(args) if isinstance(args, Mapping) else {}
        if _needs_confirm(server.spec, _raw) and not await _confirm_call(
            ctx, server.spec.name, _raw, raw_args
        ):
            return ToolResult.error(
                f"permission denied: 用户未确认执行 MCP 工具 {server.spec.name}/{_raw}"
                "（该工具在 confirm_tools 中；无人值守的任务无法确认）",
                denied=True,
            )
        result = await server.call(
            lambda session: session.call_tool(_raw, arguments=raw_args),
            kind="tool",
            label=_raw,
            replay_safe=replay_safe,
            timeout=timeout,
        )
        if isinstance(result, ToolResult):
            return result
        try:
            rendered = _render_blocks(getattr(result, "content", None))
        except Exception as exc:  # noqa: BLE001 - 渲染失败不升级为循环异常
            logger.warning("MCP 工具 '%s' 结果渲染失败: %s", _raw, type(exc).__name__)
            return ToolResult.error(f"(MCP 工具 '{_raw}' 返回了无法渲染的内容)")
        if getattr(result, "isError", False):
            return ToolResult.error(rendered)
        return ToolResult(content=rendered)

    return Tool(
        name=wrapped,
        description=getattr(tool_def, "description", None) or raw_name,
        params=_RawArgs,
        handler=handler,
        source="builtin",
        capabilities=frozenset(server.spec.capabilities),
        timeout_s=_tool_timeout_budget(timeout),
        input_schema=_normalize_schema_for_provider(
            getattr(tool_def, "inputSchema", None) or {"type": "object", "properties": {}}
        ),
    )


def _make_resource_tool(server: _ServerRuntime, resource: Any) -> Tool | None:
    uri = str(getattr(resource, "uri", "") or "")
    if not uri:
        return None
    label = f"{getattr(resource, 'name', '') or uri}"
    name = _sanitize_mcp_tool_name(f"mcp_{server.spec.name}_resource_{label}")
    timeout = server.spec.tool_timeout

    async def handler(ctx: Any, args: Any, _uri: str = uri) -> ToolResult:
        result = await server.call(
            lambda session: session.read_resource(_uri),
            kind="resource",
            label=_uri,
            replay_safe=True,
            timeout=timeout,
        )
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=_render_blocks(getattr(result, "contents", None)))

    return Tool(
        name=name,
        description=getattr(resource, "description", None) or f"MCP 资源 {uri}",
        params=_RawArgs,
        handler=handler,
        source="builtin",
        timeout_s=_tool_timeout_budget(timeout),
        input_schema={"type": "object", "properties": {}},
    )


def _make_prompt_tool(server: _ServerRuntime, prompt: Any) -> Tool | None:
    raw_name = str(getattr(prompt, "name", "") or "")
    if not raw_name:
        return None
    name = _sanitize_mcp_tool_name(f"mcp_{server.spec.name}_prompt_{raw_name}")
    properties: dict[str, Any] = {}
    required: list[str] = []
    for argument in getattr(prompt, "arguments", []) or []:
        arg_name = str(getattr(argument, "name", "") or "")
        if not arg_name:
            continue
        properties[arg_name] = {
            "type": "string",
            "description": getattr(argument, "description", None) or "",
        }
        if getattr(argument, "required", False):
            required.append(arg_name)
    timeout = server.spec.tool_timeout

    async def handler(ctx: Any, args: Any, _raw: str = raw_name) -> ToolResult:
        raw_args = dict(args) if isinstance(args, Mapping) else {}
        result = await server.call(
            lambda session: session.get_prompt(_raw, arguments=raw_args),
            kind="prompt",
            label=_raw,
            replay_safe=True,
            timeout=timeout,
        )
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=_render_prompt(result))

    return Tool(
        name=name,
        description=getattr(prompt, "description", None) or f"MCP 提示 {raw_name}",
        params=_RawArgs,
        handler=handler,
        source="builtin",
        timeout_s=_tool_timeout_budget(timeout),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


def _build_tools(server: _ServerRuntime) -> list[Tool]:
    enabled = set(server.spec.enabled_tools)
    allow_all = "*" in enabled
    tools: list[Tool] = []
    for tool_def in server.tools:
        raw_name = str(getattr(tool_def, "name", "") or "")
        if not raw_name:
            continue
        wrapped = _sanitize_mcp_tool_name(f"mcp_{server.spec.name}_{raw_name}")
        if not allow_all and raw_name not in enabled and wrapped not in enabled:
            continue
        tool = _make_tool(server, tool_def, raw_name)
        if tool is not None:
            tools.append(tool)
    if allow_all:
        for resource in server.resources:
            tool = _make_resource_tool(server, resource)
            if tool is not None:
                tools.append(tool)
        for prompt in server.prompts:
            tool = _make_prompt_tool(server, prompt)
            if tool is not None:
                tools.append(tool)
    return tools


# ---------------------------------------------------------------- 运行时


class _MCPRuntime:
    def __init__(self, servers: Mapping[str, _ServerSpec]) -> None:
        self.loop = _MCPLoop()
        self.servers = {
            name: _ServerRuntime(spec, self.loop) for name, spec in servers.items()
        }
        self._registered: list[Tool] = []

    def start(self) -> None:
        self.loop.start()
        budget = sum(
            server.spec.connect_timeout for server in self.servers.values()
        ) + 5.0
        self.loop.call(self._start_servers(), timeout=budget)

    async def _start_servers(self) -> None:
        for server in self.servers.values():
            with contextlib.suppress(BaseException):
                await server.start()

    def tools(self) -> list[Tool]:
        if not self._registered:
            seen: set[str] = set()
            for server in self.servers.values():
                if not server.connected:
                    continue
                for tool in _build_tools(server):
                    if tool.name in seen:
                        logger.warning("MCP 工具名重复，已跳过: %s", tool.name)
                        continue
                    seen.add(tool.name)
                    self._registered.append(tool)
        return list(self._registered)

    @property
    def connected(self) -> list[str]:
        return sorted(name for name, server in self.servers.items() if server.connected)

    def close(self) -> None:
        with contextlib.suppress(BaseException):
            self.loop.call(self._close_servers(), timeout=10.0)
        self.loop.stop()

    async def _close_servers(self) -> None:
        for server in self.servers.values():
            with contextlib.suppress(BaseException):
                await server.aclose()


_RUNTIMES: dict[str, _MCPRuntime] = {}
_RUNTIME_LOCK = threading.Lock()


def setup(api: Any) -> None:
    servers = _parse_servers(api.config)
    if not servers:
        return
    try:
        import mcp  # noqa: F401 - 可选依赖，缺失时跳过
    except Exception:  # noqa: BLE001
        logger.warning("已配置 MCP server 但未安装 mcp SDK，扩展跳过（pip install 'markbot[mcp]'）")
        return

    key = _signature(servers)
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = _MCPRuntime(servers)
            runtime.start()
            if not runtime.connected:
                runtime.close()
                return
            _RUNTIMES[key] = runtime

    tools = runtime.tools()
    for tool in tools:
        api.register_tool(tool)

    async def _shutdown(*args: Any, **kwargs: Any) -> None:
        with _RUNTIME_LOCK:
            _RUNTIMES.pop(key, None)
        runtime.close()

    api.on(EventName.SHUTDOWN, _shutdown)
    api.append_entry(
        "mcp_servers",
        {"servers": runtime.connected, "tools": [tool.name for tool in tools]},
    )
    logger.info(
        "MCP 已就绪：servers=%s, tools=%d",
        ",".join(runtime.connected),
        len(tools),
    )
