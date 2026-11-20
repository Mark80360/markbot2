"""Office 能力包：xlsx 读写。openpyxl 缺失时给出明确提示而非崩溃。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult


def _openpyxl():
    try:
        import openpyxl  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise RuntimeError(
            "需要 openpyxl：pip install 'markbot[office]' 或 pip install openpyxl"
        ) from exc
    return openpyxl


def _resolve(ctx, raw: str) -> Path:
    path = Path(raw).expanduser()
    root = Path(ctx.policy.root).resolve()
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"路径越界: {raw}") from exc
    return resolved


class ReadIn(BaseModel):
    path: str = Field(description="xlsx 文件路径")
    sheet: str = Field(default="", description="工作表名，留空取第一个")
    max_rows: int = Field(default=200, ge=1, le=5000)


class WriteIn(BaseModel):
    path: str = Field(description="输出 xlsx 路径")
    sheet: str = Field(default="Sheet1")
    rows: list[list[str | int | float | None]] = Field(
        default_factory=list, description="二维表数据，第一行通常为表头"
    )


class ListIn(BaseModel):
    path: str = Field(description="xlsx 文件路径")


def setup(api: ExtensionAPI) -> None:
    async def xlsx_list_sheets(ctx, args: ListIn) -> ToolResult:
        try:
            openpyxl = _openpyxl()
            path = _resolve(ctx, args.path)
            wb = openpyxl.load_workbook(path, read_only=True)
            names = list(wb.sheetnames)
            wb.close()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(str(exc))
        return ToolResult(content="\n".join(names) or "（无工作表）")

    async def xlsx_read(ctx, args: ReadIn) -> ToolResult:
        try:
            openpyxl = _openpyxl()
            path = _resolve(ctx, args.path)
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb[args.sheet] if args.sheet else wb[wb.sheetnames[0]]
            rows: list[str] = []
            for index, row in enumerate(ws.iter_rows(values_only=True)):
                if index >= args.max_rows:
                    break
                rows.append("\t".join("" if cell is None else str(cell) for cell in row))
            wb.close()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(str(exc))
        return ToolResult(content="\n".join(rows) or "（空表）", details={"rows": len(rows)})

    async def xlsx_write(ctx, args: WriteIn) -> ToolResult:
        try:
            openpyxl = _openpyxl()
            path = _resolve(ctx, args.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = args.sheet or "Sheet1"
            for row in args.rows:
                ws.append(row)
            wb.save(path)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(str(exc))
        return ToolResult(content=f"已写入 {path}（{len(args.rows)} 行）")

    api.register_tool(
        Tool(
            name="xlsx_list_sheets",
            description="列出 xlsx 工作表名",
            params=ListIn,
            handler=xlsx_list_sheets,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="xlsx_read",
            description="读取 xlsx 工作表为制表符分隔文本",
            params=ReadIn,
            handler=xlsx_read,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="xlsx_write",
            description="把二维数据写入 xlsx 文件",
            params=WriteIn,
            handler=xlsx_write,
            capabilities=frozenset({"fs.write:./**"}),
        )
    )


__all__ = ["setup"]
