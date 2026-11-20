---
name: xlsx-edit
description: 读取或生成 xlsx 表格；先把数据结构化，再写入并回读校验。
allowed-tools: [read_file, xlsx_list_sheets, xlsx_read, xlsx_write]
---

# xlsx 编辑

适用：Excel 表格读取、汇总、生成导出。

步骤：
1. 用 `xlsx_list_sheets` 确认工作表；必要时用 `xlsx_read` 读取表头与前几行。
2. 明确输出列与单位，避免含糊的「整理一下」。
3. 用 `xlsx_write` 写出新文件，不要覆盖源文件。
4. 回读输出文件，核对行数与关键单元格，再给出路径。

约束：
- 不臆造缺失数据；空值保持为空并在回复中说明。
- 超过 5000 行时先分页读取，避免一次性拉满上下文。
