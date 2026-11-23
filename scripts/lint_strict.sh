#!/usr/bin/env bash
# 严格检查（棘轮）：不阻断提交，只报告存量并和基线比较，数量只许减少。
#
#   bash scripts/lint_strict.sh            # 报告
#   bash scripts/lint_strict.sh --update   # 清理后更新基线
#
# 规则：B（bugbear：可变默认值、except 里 raise 丢失原因等）、UP（新语法）、
# SIM（可简化写法）、RUF（ruff 自带），以及 pyright basic 类型检查。
set -uo pipefail
cd "$(dirname "$0")/.."
BASELINE=scripts/.lint_baseline
RULES="B,UP,SIM,RUF"

ruff_count=$(.venv/bin/ruff check src tests channels --select "$RULES" --exit-zero --output-format concise 2>/dev/null | grep -cE '^[^ ].*:[0-9]+:[0-9]+: ' || true)
echo "ruff [$RULES]: $ruff_count 处"
.venv/bin/ruff check src tests channels --select "$RULES" --exit-zero --statistics 2>/dev/null | head -20

pyright_count=0
if [ -x .venv/bin/pyright ]; then
  pyright_count=$(.venv/bin/pyright --outputjson 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["summary"]["errorCount"])' 2>/dev/null || echo 0)
  echo "pyright basic: $pyright_count 个错误"
else
  echo "pyright 未安装：pip install -e '.[dev]'"
fi

if [ "${1:-}" = "--update" ]; then
  printf '%s %s\n' "$ruff_count" "$pyright_count" > "$BASELINE"
  echo "基线已更新：$(cat "$BASELINE")"
  exit 0
fi
if [ -f "$BASELINE" ]; then
  read -r base_ruff base_pyright < "$BASELINE"
  if [ "$ruff_count" -gt "$base_ruff" ] || [ "$pyright_count" -gt "$base_pyright" ]; then
    echo "回退：ruff $base_ruff -> $ruff_count，pyright $base_pyright -> $pyright_count" >&2
    exit 1
  fi
  echo "未回退（基线 ruff=$base_ruff pyright=$base_pyright）"
else
  echo "还没有基线：确认后运行 bash scripts/lint_strict.sh --update"
fi
