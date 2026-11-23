"""受限命令执行工具（默认需要用户确认）。

命令通过 ``ctx.env.exec`` 执行；执行环境决定沙箱、工作目录与超时策略。

密钥注入：模型在 ``secrets`` 里写密钥名，命令里用 ``$NAME`` 引用；值由工具在执行时
以环境变量注入，模型、会话归档与日志里只出现名称。能注入哪些密钥、给哪些程序用，
由 ``[permissions.secret_env]`` 决定（``PermissionPolicy.check_secret_injection``）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

# 命令自身的超时上限。外层工具超时必须比它长：让内层先超时、杀掉进程组并带回已有输出，
# 而不是外层先取消（旧值外层 120s、内层最长 600s，长命令总是被外层打断）。
MAX_COMMAND_TIMEOUT_S = 600.0
_OUTER_MARGIN_S = 15.0


class RunIn(BaseModel):
    command: str = Field(
        description="要执行的 shell 命令；需要密钥时用 \"$NAME\" 引用",
        json_schema_extra={"x-markbot-role": "command"},
    )
    timeout_s: float = Field(default=30.0, ge=1, le=600, description="超时秒数")
    secrets: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="需要注入为环境变量的密钥名（只写名称，不写值）",
    )


def _injectable(config) -> list[str]:
    permissions = (config or {}).get("permissions") or {}
    raw = permissions.get("secret_env") or {}
    names = raw if isinstance(raw, (list, tuple)) else list(raw)
    return sorted(str(name) for name in names if str(name).strip())


def _description(names: list[str]) -> str:
    base = "在受限环境中执行 shell 命令并返回输出。"
    if not names:
        return base + "当前没有可注入的密钥；不要读取配置文件或日志去找密钥值。"
    example = names[0]
    return (
        base
        + "需要密钥时把密钥名放进 secrets，命令里用 \"$NAME\" 引用，例如 "
        + f"secrets=[\"{example}\"]、command='curl -H \"Authorization: Bearer ${example}\" …'。"
        + "密钥值由系统在执行时注入，你看不到也不需要知道；不要把密钥值写进命令、文件或回复，"
        + "不要回显、编码、拆分或还原被遮蔽为 [REDACTED] 的内容。"
        + f"可注入的密钥：{'、'.join(names)}。"
    )


def setup(api: ExtensionAPI) -> None:
    async def run_command(ctx, args: RunIn) -> ToolResult:
        env: dict[str, str] = {}
        names = list(dict.fromkeys(name.strip() for name in args.secrets if name.strip()))
        if names:
            check = getattr(ctx.policy, "check_secret_injection", None)
            if check is None:
                return ToolResult.error("当前权限策略不支持密钥注入", denied=True)
            decision = check(names, args.command)
            if not decision.allowed:
                return ToolResult.error(f"permission denied: {decision.reason}", denied=True)
            for name in names:
                try:
                    env[name] = ctx.secret(name)
                except KeyError:
                    return ToolResult.error(
                        f"未配置密钥: {name}（在 config.toml 的 [secrets] 中添加）"
                    )
        timeout_s = min(args.timeout_s, MAX_COMMAND_TIMEOUT_S)
        if env:
            result = await ctx.env.exec(args.command, timeout_s=timeout_s, env=env)
        else:
            # 不需要注入时保持旧调用形状，兼容未实现 env 参数的执行环境
            result = await ctx.env.exec(args.command, timeout_s=timeout_s)
        content = result.combined()
        details = {"returncode": result.returncode}
        if names:
            details["secrets"] = names
        if not result.ok:
            details["timed_out"] = result.timed_out
            return ToolResult(
                content=content or f"命令失败，退出码 {result.returncode}",
                is_error=True,
                details=details,
            )
        return ToolResult(content=content or "（无输出）", details=details)

    api.register_tool(
        Tool(
            name="run_command",
            description=_description(_injectable(api.config)),
            params=RunIn,
            handler=run_command,
            capabilities=frozenset({"exec:*"}),
            timeout_s=MAX_COMMAND_TIMEOUT_S + _OUTER_MARGIN_S,
        )
    )


__all__ = ["RunIn", "setup"]
