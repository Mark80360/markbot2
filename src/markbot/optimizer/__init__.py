"""离线优化器：只写 learned/，可审计、可回滚。"""

from markbot.optimizer.adapter import AssistantAdapter, BenchSpec, SessionStore
from markbot.optimizer.evals import Check, EvalCase, EvalSuite, Score, load_cases, score_text
from markbot.optimizer.gate import GateResult, evaluate, illegal_paths
from markbot.optimizer.run import OptimizationReport, OptimizerSettings, optimize
from markbot.optimizer.snapshots import (
    create_snapshot,
    list_snapshots,
    promote,
    read_manifest,
    read_tree,
    restore_snapshot,
    write_tree,
)

__all__ = [
    "AssistantAdapter",
    "BenchSpec",
    "Check",
    "EvalCase",
    "EvalSuite",
    "GateResult",
    "OptimizationReport",
    "OptimizerSettings",
    "Score",
    "SessionStore",
    "create_snapshot",
    "evaluate",
    "illegal_paths",
    "list_snapshots",
    "load_cases",
    "optimize",
    "promote",
    "read_manifest",
    "read_tree",
    "restore_snapshot",
    "score_text",
    "write_tree",
]
