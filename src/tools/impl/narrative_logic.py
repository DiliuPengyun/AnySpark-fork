# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 徐俊瑞 (Junrui Xu). Commercial licensing rights reserved.

"""Tool handlers for the narrative logic engine.

These are thin wrappers that extract parameters, call the
narrative_logic package, and format the result for the agent.
"""

from __future__ import annotations

import json
import logging

from core.llm_client import chat as llm_chat
from core.narrative_logic import (
    ConfidenceScorer,
    ConstraintChecker,
    ConstraintStore,
    ImpactPropagator,
    ImpactSource,
)
from core.thread_pools import llm_pool as _ai_executor
from core.utils import extract_json_from_response

logger = logging.getLogger(__name__)


# ── Constraint definition ──

async def _define_constraint(loop, args: dict, kb, book_id: str, msg: str = "") -> str:
    """Convert a natural-language constraint into a structured rule and store it."""
    description = args.get("description", "").strip()
    if not description:
        return "错误: 需要描述约束规则"

    severity = args.get("severity", "hard")
    if severity not in ("hard", "soft"):
        severity = "hard"

    # Use LLM to classify the constraint and generate a Cypher violation query
    system = """你是小说叙事约束分析专家。将用户的自然语言约束转换为结构化规则。

分析约束类型:
- entity_state: 实体状态约束（如"X归Y所有"、"X在Y地点"）
- relation_lock: 关系锁定约束（如"A和B是盟友"）
- temporal_order: 时序约束（如"事件X在事件Y之前"）
- custom: 自定义约束

生成一个只读Cypher查询（仅MATCH/RETURN，禁止CREATE/DELETE/SET），用于检测违反此约束的情况。
查询中使用 $pid 作为项目ID参数。

输出JSON:
{
  "constraint_type": "entity_state|relation_lock|temporal_order|custom",
  "target_entity": "约束涉及的主要实体名（如无则为空字符串）",
  "condition": {"key": "value"},  // 结构化条件
  "violation_query": "MATCH ... RETURN ..."  // 检测违反的Cypher查询
}
"""

    # Build context: list existing entity names so the LLM can reference them
    entities = kb.list_entities()
    entity_names = [e.name for e in entities[:50]]  # cap at 50 for token budget
    context = f"知识库中的实体（前50个）: {', '.join(entity_names)}\n" if entity_names else ""

    prompt = f"{context}用户约束: {description}"

    try:
        response = await loop.run_in_executor(
            _ai_executor, llm_chat, prompt, system, 0.1, "extraction"
        )
        parsed = extract_json_from_response(response)
        if not parsed:
            # Fallback: store as custom with no violation query
            parsed = {
                "constraint_type": "custom",
                "target_entity": "",
                "condition": {},
                "violation_query": "",
            }
    except Exception as e:
        logger.warning("LLM constraint parsing failed: %s", e)
        parsed = {
            "constraint_type": "custom",
            "target_entity": "",
            "condition": {},
            "violation_query": "",
        }

    # Store the constraint
    store = ConstraintStore(kb)
    constraint = store.add(
        description=description,
        constraint_type=parsed.get("constraint_type", "custom"),
        target_entity=parsed.get("target_entity", ""),
        condition=parsed.get("condition", {}),
        violation_query=parsed.get("violation_query", ""),
        severity=severity,
    )

    lines = [
        f"已创建叙事约束 #{constraint.id}",
        f"  规则: {description}",
        f"  类型: {constraint.constraint_type}",
        f"  严重度: {severity}",
    ]
    if constraint.violation_query:
        lines.append(f"  检测查询: {constraint.violation_query[:100]}...")
        lines.append("系统将在 check_constraints 时自动执行检测。")
    else:
        lines.append("  (未生成自动检测查询，此约束仅作记录)")
    lines.append(f"\n当前共有 {len(store.list())} 条活跃约束。")

    return "\n".join(lines)


# ── Constraint checking ──

async def _check_constraints(loop, args: dict, kb, book_id: str, msg: str = "") -> str:
    """Check all active constraints and report violations."""
    checker = ConstraintChecker(kb)
    store = ConstraintStore(kb)
    constraints = store.list(active_only=True)

    if not constraints:
        return "当前没有活跃的叙事约束。使用 define_constraint 创建约束。"

    violations = checker.check_all()

    lines = [f"检查了 {len(constraints)} 条叙事约束:"]

    if not violations:
        lines.append("所有约束全部通过。")
    else:
        lines.append(f"发现 {len(violations)} 条约束被违反:\n")
        for v in violations:
            sev_icon = {"hard": "🔴", "soft": "🟡"}.get(v.severity, "⚪")
            lines.append(f"{sev_icon} 约束#{v.constraint_id}: {v.description}")
            for detail in v.violations[:5]:
                lines.append(f"   - {json.dumps(detail, ensure_ascii=False)}")
            if len(v.violations) > 5:
                lines.append(f"   ... 还有 {len(v.violations) - 5} 条违反")

    return "\n".join(lines)


# ── Impact analysis ──

async def _analyze_impact(loop, args: dict, kb, book_id: str, msg: str = "") -> str:
    """Analyze the blast radius of modifying a graph element."""
    source_type = args.get("source_type", "entity")
    source_id = args.get("source_id", "").strip()
    change_desc = args.get("change_description", "")

    if not source_id:
        return "错误: 需要 source_id 参数"

    if source_type not in ("entity", "timeline_event", "foreshadow"):
        return f"错误: source_type 必须是 entity / timeline_event / foreshadow，当前为 {source_type}"

    propagator = ImpactPropagator(kb)
    source = ImpactSource(
        source_type=source_type,
        source_id=source_id,
        description=change_desc,
    )
    report = propagator.propagate(source)

    sev_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(report.max_severity, "⚪")

    lines = [
        f"影响传播分析 {sev_icon} (爆炸半径: {report.blast_radius} 个节点, 严重度: {report.max_severity})",
    ]
    if change_desc:
        lines.append(f"  改动: {change_desc}")

    if report.directly_affected:
        lines.append(f"\n🔴 直接影响 ({len(report.directly_affected)} 个):")
        for item in report.directly_affected[:10]:
            lines.append(f"   - {item['name']} ({item['type']}, 权重={item['weight']})")

    if report.indirectly_affected:
        lines.append(f"\n🟡 间接影响 ({len(report.indirectly_affected)} 个):")
        for item in report.indirectly_affected[:10]:
            lines.append(f"   - {item['name']} ({item['type']}, 权重={item['weight']}, {item.get('path_length', '?')}跳)")

    if report.affected_chapters:
        lines.append(f"\n📖 受影响时间线事件 ({len(report.affected_chapters)} 个):")
        for ch in report.affected_chapters[:5]:
            lines.append(f"   - {ch.get('label', '?')} (章节: {ch.get('chapter_ref', '?')})")

    if report.affected_foreshadows:
        lines.append(f"\n🔮 受影响伏笔 ({len(report.affected_foreshadows)} 个):")
        for fs in report.affected_foreshadows[:5]:
            status = "已回收" if fs.get("resolved") else "未回收"
            lines.append(f"   - {fs.get('text', '?')[:30]}... ({status})")

    if report.blast_radius == 0:
        lines.append("\n未找到关联节点，此改动影响范围极小。")

    return "\n".join(lines)


# ── Confidence scoring ──

async def _score_confidence(loop, args: dict, kb, book_id: str, msg: str = "") -> str:
    """Score the confidence/health of knowledge entities."""
    entity_id = args.get("entity_id", "").strip()
    scorer = ConfidenceScorer(kb)

    if entity_id:
        score = scorer.score_one(entity_id)
        scores = [score]
    else:
        scores = scorer.score_all()

    if not scores:
        return "知识库为空，没有可评分的实体。"

    lines = [f"设定可信度评分 ({len(scores)} 个实体):\n"]

    for s in scores[:30]:
        stars = "★" * s.stars + "☆" * (5 - s.stars)
        lines.append(
            f"{stars} {s.entity_name} ({s.entity_type}) "
            f"[{s.confidence}] - {s.chapter_mentions}次引用, "
            f"{s.relation_count}条关系, {s.contradiction_count}个矛盾"
        )
        if s.recommendation != "设定充足":
            lines.append(f"   → {s.recommendation}")

    if len(scores) > 30:
        lines.append(f"\n... 还有 {len(scores) - 30} 个实体未显示")

    # Summary
    avg = sum(s.confidence for s in scores) / len(scores)
    low_count = sum(1 for s in scores if s.confidence < 0.3)
    high_count = sum(1 for s in scores if s.confidence >= 0.5)
    lines.append(f"\n平均可信度: {avg:.3f} | 高分(>=0.5): {high_count} | 低分(<0.3): {low_count}")

    return "\n".join(lines)


# ── Delete constraint (bonus utility) ──

async def _delete_constraint(loop, args: dict, kb, book_id: str, msg: str = "") -> str:
    """Delete a narrative constraint by ID."""
    cid = args.get("constraint_id", "").strip()
    if not cid:
        return "错误: 需要 constraint_id 参数"

    store = ConstraintStore(kb)
    existing = store.get(cid)
    if not existing:
        return f"错误: 约束 {cid} 不存在"

    store.delete(cid)
    remaining = store.list(active_only=True)
    return f"已删除约束 #{cid} ({existing.description})\n剩余活跃约束: {len(remaining)} 条"
