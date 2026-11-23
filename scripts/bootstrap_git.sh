#!/usr/bin/env bash
# 把 markbot2 纳入 git，并装上提交前门禁。只需运行一次。
#
#   bash scripts/bootstrap_git.sh
#
# 做的事：
#   1. git init，确认运行时数据（.markbot/、secrets.toml、auth.token）被忽略；
#   2. 暂存后扫描疑似密钥，发现就中止（不会提交）；
#   3. 提交当前代码作为基线；
#   4. 安装 pre-commit 钩子：ruff + pytest 全绿才能提交（AGENTS.md 的门禁）；
#   5. 把 .patch_backup/ 打包到仓库外，删除与否由你决定。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [ -d .git ]; then
  echo "已经是 git 仓库：$ROOT，跳过初始化。"
else
  git init -b main >/dev/null
  echo "已初始化 git 仓库：$ROOT"
fi

for path in .markbot/config.toml .markbot/secrets.toml .markbot/gateway/auth.token .patch_backup/x; do
  if ! git check-ignore -q "$path"; then
    echo "error: $path 没有被 .gitignore 忽略，中止，避免把运行时数据/密钥提交进仓库。" >&2
    exit 1
  fi
done

git add -A

# 疑似密钥：OpenAI/Anthropic 风格 key、飞书 app secret、私钥头
# tests/ 里有脱敏用例专用的假 key（sk-test…），不参与扫描
PATTERN='(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
if git diff --cached --name-only -z -- . ':(exclude)tests/' | xargs -0 grep -nIE "$PATTERN" -- 2>/dev/null; then
  echo "error: 暂存区里发现疑似密钥（见上），已中止。请移除后重试：git reset" >&2
  git reset -q
  exit 1
fi

if git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "已有提交，跳过基线提交。"
else
  git -c user.name="${GIT_AUTHOR_NAME:-Mark Tang}" -c user.email="${GIT_AUTHOR_EMAIL:-markbot@localhost}" \
    commit -q -m "chore: baseline import of markbot2 (phase-1 security hardening included)"
  echo "已提交基线：$(git rev-parse --short HEAD)"
fi

HOOK=.git/hooks/pre-commit
cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
# AGENTS.md 门禁：ruff + pytest 全绿才能提交
set -e
cd "$(git rev-parse --show-toplevel)"
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
EOF
chmod +x "$HOOK"
echo "已安装提交前门禁：$HOOK"

if [ -d .patch_backup ]; then
  ARCHIVE="$HOME/markbot2-patch-backup-$(date +%Y%m%d%H%M%S).tar.gz"
  tar -czf "$ARCHIVE" .patch_backup
  chmod 600 "$ARCHIVE"
  echo "已把 .patch_backup/ 打包到 $ARCHIVE"
  echo "确认无误后可删除：rm -rf $ROOT/.patch_backup"
fi
