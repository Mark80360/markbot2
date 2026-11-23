"""优化器主流程：Propose -> Probe -> Compose -> Gate -> Snapshot -> Promote。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from markbot.optimizer.adapter import SessionStore
from markbot.optimizer.evals import EvalCase, EvalSuite, Score
from markbot.optimizer.gate import evaluate
from markbot.optimizer.snapshots import LearnedConflict, promote, read_tree, write_tree
from markbot.schema.hashing import tree_hash

PROPOSER_SYSTEM = """你是 MarkBot 的离线优化器。你会看到当前学习层和已完成的会话摘要。
你的任务是改进系统提示词，让下一次会话更可靠。
硬约束：
- 只输出一个 ```markdown 代码块，内容是完整的 system_prompt.md。
- 不得新增权限、工具或外部依赖。
- 不得要求绕过确认、不得编造工具结果。
- 保持简洁，优先写可验证的行为约束。
"""


class ProviderLike(Protocol):
    async def complete(self, payload: dict[str, Any]) -> Any: ...


@dataclass
class OptimizerSettings:
    enabled: bool = True
    k: int = 1
    max_rollouts_per_task: int = 4
    max_sessions_per_task: int = 3
    max_tasks_per_run: int = 5
    idle_minutes: int = 30
    # 回归集少于这么多条时不晋升：4 条 contains 检查几乎永远「不掉分」，门禁形同虚设
    min_eval_cases: int = 10
    # 新增/修改技能（skills/**）的候选默认不自动晋升，放进 optimizer/pending/ 等人工确认
    approve_skills: bool = False
    # 可选的 LLM 评分器：judge(answer, rubric) -> bool，用于 llm_judge 检查
    judge: Any = None
    # 额外的晋升门禁评测集（ADR 0016）：名字 -> async runner(harness_dir) -> Score。
    # 每个评测集都只许变好；由 CLI 按 [optimizer] gate_suites 组装（编码、浏览器评测）
    extra_suites: dict[str, Any] = field(default_factory=dict)
    gate_tolerance: float = 0.0


@dataclass
class OptimizationReport:
    run_id: str
    tasks: int
    dry_run: bool
    promoted: bool = False
    selected: str | None = None
    reasons: list[str] = field(default_factory=list)
    base_score: str = "0/0"
    candidate_score: str = "0/0"
    parent_snapshot: str | None = None
    new_snapshot: str | None = None
    # learned/ 由门禁自动信任，但同样要留下审计记录（7.2 规则 4）。
    learned_auto_trust: bool = False
    proposals: list[str] = field(default_factory=list)
    # 门禁里各评测集的「当前 -> 候选」分数（ADR 0016）
    suite_scores: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:4]}"


async def _ask(provider: ProviderLike, prompt: str) -> str:
    reply = await provider.complete(
        {
            "system": PROPOSER_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [],
        }
    )
    return getattr(reply, "text", "") or ""


def _fenced(text: str) -> str:
    if "```" not in text:
        return ""
    parts = text.split("```")
    if len(parts) < 3:
        return ""
    body = parts[1]
    lines = body.splitlines()
    if lines and lines[0].strip().lower() in {"markdown", "md", "text", "toml"}:
        lines = lines[1:]
    return "\n".join(lines).strip()


def summarize_sessions(store: SessionStore, metas: list[Any], limit: int = 3) -> str:
    rows: list[str] = []
    for meta in metas[-limit:]:
        try:
            request = store.read(meta.id, "request.md").strip()[:400]
        except OSError:
            request = ""
        trajectory = store.trajectory(meta.id)
        errors = [
            row
            for row in trajectory
            if row.get("role") == "tool" and row.get("is_error")
        ]
        tools = sorted(
            {
                call.get("name", "")
                for row in trajectory
                for call in (row.get("tool_calls") or [])
                if call.get("name")
            }
        )
        rows.append(
            f"- [{meta.id}] domain={meta.domain} steps={meta.steps} tools={meta.tool_calls}\n"
            f"  request: {request}\n"
            f"  tool_errors: {len(errors)} used_tools: {', '.join(tools) or 'none'}"
        )
    return "\n".join(rows) if rows else "（没有已完成的会话）"


def _fallback_prompt(current: str, summary: str) -> str:
    # 不把会话摘要写进系统提示：摘要里有用户请求原文，晋升后会永久进入每一轮上下文
    base = current.strip() or "你是 MarkBot，一个边界清晰、可审计的个人助手。"
    if "## 经验约束" in base:
        return base + "\n"
    return (
        f"{base}\n\n## 经验约束\n\n"
        "- 先确认目标与约束，再调用工具；信息不足时说明缺口。\n"
        "- 工具失败时如实报告，并改用可验证的替代路径。\n"
        "- 交付物落盘，回复中给出路径与关键结论。\n"
    )


async def propose_inv(current_tree: dict[str, str], summary: str, provider: ProviderLike) -> dict[str, str]:
    current = current_tree.get("strategy/system_prompt.md", "")
    prompt = (
        f"当前 system_prompt.md:\n\n{current or '（空）'}\n\n"
        f"最近会话摘要:\n\n{summary}\n\n"
        "请输出改进后的完整 system_prompt.md。"
    )
    improved = _fenced(await _ask(provider, prompt)) or _fallback_prompt(current, summary)
    if improved.strip() == current.strip():
        improved = improved.rstrip() + "\n\n- 保持工具结果与结论可验证。"
    tree = dict(current_tree)
    tree["strategy/system_prompt.md"] = improved.strip() + "\n"
    return tree


def propose_dom(current_tree: dict[str, str], summary: str, domain: str) -> dict[str, str]:
    name = f"{domain}-playbook"
    body = (
        "---\n"
        f"name: {name}\n"
        f"description: {domain} 领域的可复用执行清单。当任务属于 {domain} 领域时使用。\n"
        "allowed-tools: []\n"
        "---\n\n"
        f"# {name}\n\n"
        "1. 先确认输入文件与期望产出。\n"
        "2. 用工具读取源数据，不臆测内容。\n"
        "3. 产出结构化结果并落盘。\n"
        "4. 在回复中给出路径与校验结论。\n"
    )
    tree = dict(current_tree)
    tree[f"skills/{name}/SKILL.md"] = body
    memory = tree.get("strategy/memory.md", "").rstrip()
    note = f"- 领域 {domain}：优先复用 {name} 技能的执行清单。"
    if note not in memory:
        tree["strategy/memory.md"] = (memory + "\n" + note).strip() + "\n"
    return tree


async def optimize(
    *,
    home: Path,
    learned_dir: Path,
    evals_dir: Path,
    provider: ProviderLike,
    run_case: Any,
    settings: OptimizerSettings | None = None,
    dry_run: bool = False,
    emit=print,
) -> OptimizationReport:
    settings = settings or OptimizerSettings()
    home = Path(home)
    learned_dir = Path(learned_dir)
    run_id = _run_id()
    started = _now()
    report = OptimizationReport(run_id=run_id, tasks=0, dry_run=dry_run)

    store = SessionStore(home)
    metas = store.completed(before=started, limit=settings.max_tasks_per_run)
    report.tasks = len(metas)
    summary = summarize_sessions(store, metas, limit=settings.max_sessions_per_task)
    current_tree = read_tree(learned_dir)
    # 候选基于这个版本生成；晋升时若 learned/ 已被其它写入方修改则放弃，避免覆盖丢失
    base_hash = tree_hash(learned_dir) if learned_dir.exists() else ""

    candidates = {
        "inv": await propose_inv(current_tree, summary, provider),
        "dom": propose_dom(current_tree, summary, metas[-1].domain if metas else "general"),
    }

    runs_dir = home / "optimizer" / "runs" / run_id
    candidate_dirs: dict[str, Path] = {}
    for kind, tree in candidates.items():
        directory = runs_dir / "candidates" / kind
        write_tree(directory, tree)
        candidate_dirs[kind] = directory
    write_tree(runs_dir / "base", current_tree)
    report.proposals = sorted(candidates)

    if not evals_dir or not Path(evals_dir).exists():
        report.reasons.append("没有回归集，门禁无法判定，未晋升")
        _audit(home, report)
        return report

    cases: list[EvalCase] = settings_cases(evals_dir)
    if len(cases) < settings.min_eval_cases:
        report.reasons.append(
            f"回归集只有 {len(cases)} 条，少于 min_eval_cases={settings.min_eval_cases}，"
            "门禁无法有效判定，未晋升（往 evals/cases/ 补充用例）"
        )
        _audit(home, report)
        return report
    suite = EvalSuite(cases, run_case, judge=settings.judge)
    base_dir = learned_dir if learned_dir.exists() else runs_dir / "base"
    base_score: Score = await suite.score(base_dir)
    report.base_score = str(base_score)

    scored: list[tuple[str, Score]] = []
    for kind, directory in candidate_dirs.items():
        scored.append((kind, await suite.score(directory)))
    emit("回归评分：" + "，".join(f"{kind}={score}" for kind, score in scored))

    eligible = [(kind, score) for kind, score in scored if score.ratio >= base_score.ratio]
    if not eligible:
        report.reasons.append(f"所有候选都低于基线 {base_score}")
        for kind, _ in scored:
            _reject(home, run_id, kind, candidate_dirs[kind])
        _audit(home, report)
        return report

    kind, cand_score = max(eligible, key=lambda item: item[1].ratio)
    report.selected = kind
    report.candidate_score = str(cand_score)
    candidate_dir = candidate_dirs[kind]

    if tree_hash(candidate_dir) == tree_hash(base_dir):
        report.reasons.append("候选与当前 harness 完全相同")
        _reject(home, run_id, kind, candidate_dir)
        _audit(home, report)
        return report

    gate = await evaluate(
        candidate_dir,
        base_dir,
        suite.score,
        extra_suites=settings.extra_suites,
        tolerance=settings.gate_tolerance,
    )
    report.suite_scores = dict(gate.scores)
    if not gate.passed:
        report.reasons.extend(gate.reasons)
        for name, directory in candidate_dirs.items():
            _reject(home, run_id, name, directory)
        _audit(home, report)
        return report

    if dry_run:
        report.reasons.append("dry-run：通过门禁但未晋升")
        _audit(home, report)
        return report

    touched = _skill_changes(current_tree, candidates[kind])
    if touched and not settings.approve_skills:
        pending = home / "optimizer" / "pending" / f"{run_id}-{kind}"
        write_tree(pending, candidates[kind])
        report.reasons.append(
            f"候选修改了技能（{', '.join(touched)}），需要人工确认；已保存到 {pending}。"
            "确认后运行 `markbot optimize --approve-skills`"
        )
        _audit(home, report)
        return report

    try:
        parent, new_hash = promote(
            candidates[kind],
            learned_dir,
            home / "snapshots",
            origin_task=metas[-1].id if metas else None,
            expected_hash=base_hash,
        )
    except LearnedConflict as exc:
        report.reasons.append(f"{exc}（记忆在评估期间有新写入），下次运行重试")
        _audit(home, report)
        return report
    report.promoted = True
    report.parent_snapshot = parent
    report.new_snapshot = new_hash
    report.learned_auto_trust = True
    report.reasons.append("门禁通过，已晋升；learned/ 自动信任并记录审计")
    _audit(home, report)
    return report


def settings_cases(directory: Path) -> list[EvalCase]:
    from markbot.optimizer.evals import load_cases

    return load_cases(directory)


def _skill_changes(current: dict[str, str], candidate: dict[str, str]) -> list[str]:
    """候选相对当前学习层新增、修改或删除的技能文件。"""
    paths = {p for p in (*current, *candidate) if p.startswith("skills/")}
    return sorted(p for p in paths if current.get(p) != candidate.get(p))


def _reject(home: Path, run_id: str, kind: str, candidate_dir: Path) -> None:
    target = home / "optimizer" / "rejected" / f"{run_id}-{kind}"
    write_tree(target, read_tree(candidate_dir))


def _audit(home: Path, report: OptimizationReport) -> None:
    path = home / "logs" / "optimizer.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report.as_dict(), ensure_ascii=False) + "\n")


__all__ = [
    "OptimizationReport",
    "OptimizerSettings",
    "optimize",
    "propose_dom",
    "propose_inv",
    "summarize_sessions",
]
