# office 能力包

本地第三方能力包示例，演示车道 C（分发包）的完整形态：

- `markbot.toml` 声明包身份、能力上限与资源路径。
- `extensions/xlsx.py` 提供 `xlsx_list_sheets`、`xlsx_read`、`xlsx_write`。
- `skills/xlsx-edit/SKILL.md` 只把索引注入上下文，正文按需读取。
- `prompts/weekly-report.md` 追加领域约束。

首次加载前需要显式信任：

```bash
markbot trust office
```

如果需要 xlsx 功能：

```bash
pip install 'markbot[office]'
```
