# 编码评测集

`markbot eval --suite coding` 在这里的每个用例上运行一个回合：把 `repo/` 复制到临时目录作为工作区，
让 Agent 按 `case.yaml` 的 `prompt` 修改代码，然后运行 `check`（默认 `python -m pytest -q`）判分；
`allowed` 之外的文件被修改也算失败。

新增用例：建一个目录，放 `case.yaml` 与 `repo/`（含能复现问题的失败测试）。
当前 5 个用例：off-by-one、safe-divide、slugify-feature、multi-file-rename、parse-config。
