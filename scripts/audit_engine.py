#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mcd-audit-officer · 稽核引擎

把内控审计的穿行测试方法，落到"订餐报销"这个具体场景：
  申报单（应然/自述） × 订单事实（实然/系统） × 官方核价（基准/权威）
三方交叉，产出带证据链和缺陷分级的稽核底稿。

设计原则：
1. 纯标准库，零第三方依赖 —— clone 下来就能跑。
2. 可离线复现 —— 没有 MCP Token 时用内置脱敏样例数据，审计结论可复核、可重放。
3. 每条缺陷都必须带 evidence —— 没有证据不下结论，这是审计的底线。
4. 规则与数据分离 —— 政策阈值集中在 Policy，规则集中在 RULES。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------- 缺陷分级

MAJOR = "重大"        # 涉及真实性、可能导致资产损失
SIGNIFICANT = "重要"  # 涉及准确性、影响费用真实性判断
MINOR = "一般"        # 涉及合规性与时效性

SEVERITY_ORDER = {MAJOR: 0, SIGNIFICANT: 1, MINOR: 2}


@dataclass
class Policy:
    """稽核政策阈值。全部可配置，默认值仅为演示口径。"""

    per_claim_cap: float = 80.0           # 单笔申报上限（元）
    per_person_daily_cap: float = 150.0   # 单人单日上限（元）
    dept_monthly_cap: float = 8000.0      # 部门单月上限（元）
    price_tolerance_abs: float = 1.0      # 价差绝对容差（元）
    price_tolerance_pct: float = 0.02     # 价差相对容差
    allowed_start: time = time(7, 0)      # 允许下单起始（含早餐时段）
    allowed_end: time = time(21, 0)       # 允许下单截止
    filing_window_days: int = 30          # 申报时限（天）
    coupon_check_min_amount: float = 40.0  # R02 券核查的最低申报金额
    require_purpose: bool = True          # 是否要求填写事由

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        p = cls()
        for k, v in d.items():
            if k in ("allowed_start", "allowed_end") and isinstance(v, str):
                hh, mm = v.split(":")
                setattr(p, k, time(int(hh), int(mm)))
            elif hasattr(p, k):
                setattr(p, k, v)
        return p


@dataclass
class Claim:
    """一笔报销申报（应然侧）。"""

    claim_id: str
    employee: str
    dept: str
    order_no: str
    order_date: str            # YYYY-MM-DD
    claim_date: str            # YYYY-MM-DD
    declared_amount: float
    declared_items: list[str] = field(default_factory=list)
    purpose: str = ""
    meal_type: str = "个人"     # 个人 / 团餐

    @property
    def order_dt(self) -> date | None:
        return _parse_date(self.order_date)

    @property
    def claim_dt(self) -> date | None:
        return _parse_date(self.claim_date)


@dataclass
class OrderFact:
    """订单事实（实然侧）。来自 MCP query-order / order-list，或样例数据。"""

    order_no: str
    store_id: str = ""
    store_name: str = ""
    order_time: str = ""        # "YYYY-MM-DD HH:MM"
    paid_amount: float | None = None
    items: list[str] = field(default_factory=list)
    coupons_used: list[str] = field(default_factory=list)
    channel: str = ""           # 到店 / 麦乐送 / 团餐
    source: str = "sample"      # mcp / sample

    @property
    def dt(self) -> datetime | None:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(self.order_time, fmt)
            except ValueError:
                continue
        return None


@dataclass
class Finding:
    """一条缺陷。必须带 evidence。"""

    rule_id: str
    rule_name: str
    severity: str
    claim_id: str
    employee: str
    dept: str
    expected: str
    actual: str
    delta: str
    evidence: list[str]
    suggestion: str


@dataclass
class AuditReport:
    scope: str = ""
    generated_at: str = ""
    policy: Policy = field(default_factory=Policy)
    claims_total: int = 0
    claims_amount: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)   # 未取到订单事实的申报
    data_source: str = "sample"

    @property
    def by_severity(self) -> dict[str, list[Finding]]:
        out = {MAJOR: [], SIGNIFICANT: [], MINOR: []}
        for f in self.findings:
            out.setdefault(f.severity, []).append(f)
        return out

    @property
    def exposure(self) -> float:
        """可疑金额敞口：价差 + 超预算部分，仅统计可量化项。"""
        total = 0.0
        for f in self.findings:
            if f.rule_id in ("R01", "R03") and f.delta:
                try:
                    total += abs(float(f.delta.replace("元", "").strip()))
                except ValueError:
                    pass
        return round(total, 2)


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _money(x: float) -> str:
    return f"{x:.2f}"


# ---------------------------------------------------------------- 控制点台账

CONTROL_POINTS = [
    ("R01", "价差异常", "申报金额与官方核价一致", SIGNIFICANT,
     "calculate-price 按同品项同券组合重算应付总额，与申报金额比对"),
    ("R02", "券未核销", "可用券应充分使用", MINOR,
     "query-my-coupons / query-store-coupons 比对该笔订单已核销券"),
    ("R03", "超预算", "单笔/单人单日/部门单月不超标准", SIGNIFICANT,
     "按 Policy 阈值逐级汇总比对"),
    ("R04", "串项", "申报品项与订单实际构成一致", SIGNIFICANT,
     "query-meal-detail 取套餐构成，与申报品项集合比对"),
    ("R05", "重复报销", "同一订单不得多次申报", MAJOR,
     "申报表内按 order_no 分组，出现次数 > 1 即命中"),
    ("R06", "非工时订单", "下单时间落在允许时段内", MINOR,
     "订单时间与 Policy.allowed_start / allowed_end 比对"),
    ("R07", "超期申报", "申报日期在订单发生后规定天数内", MINOR,
     "claim_date - order_date 与 Policy.filing_window_days 比对"),
]


# ---------------------------------------------------------------- 规则实现
# 签名统一：(claim, order, ctx) -> Finding | None
# ctx 提供：policy, benchmark（应然价格）, coupons（可用券）, meal_detail, order_index

def rule_price_variance(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    policy: Policy = ctx["policy"]
    benchmark = ctx.get("benchmark")
    actual_paid = order.paid_amount if order.paid_amount is not None else None

    # 应然基准优先级：官方核价 > 订单实付 > 申报金额（无可比基准则不判定）
    if benchmark is None:
        if actual_paid is None:
            return None
        benchmark = actual_paid

    delta = claim.declared_amount - benchmark
    tolerance = max(policy.price_tolerance_abs, abs(benchmark) * policy.price_tolerance_pct)
    if abs(delta) <= tolerance:
        return None

    severity = SIGNIFICANT
    if delta > 0 and abs(delta) >= max(20.0, abs(benchmark) * 0.2):
        severity = MAJOR  # 虚报比例显著，上升为重大

    return Finding(
        rule_id="R01",
        rule_name="价差异常",
        severity=severity,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected=f"官方核价 {_money(benchmark)} 元",
        actual=f"申报 {_money(claim.declared_amount)} 元",
        delta=f"{_money(delta)}",
        evidence=[
            f"订单号 {claim.order_no}",
            f"容差 ±{_money(tolerance)} 元（绝对 {policy.price_tolerance_abs} / 相对 {policy.price_tolerance_pct:.0%}）",
            f"核价来源：{'MCP calculate-price' if ctx.get('benchmark') is not None else '订单实付'}",
            f"订单数据源：{order.source}",
        ],
        suggestion=("退回补充有效支付凭证并说明差额原因；如无法解释，按虚报处理。"
                    if delta > 0 else "核实是否漏报，补充完整票据。"),
    )


def rule_coupon_unused(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    """
    券未核销。

    两条降噪约束（真实审计不会为小额订单纠结用券）：
    1. 申报金额低于 coupon_check_min_amount 的不判定；
    2. 同一人同一天最多报一条，避免同一批券被重复计数。
    """
    policy: Policy = ctx["policy"]
    if claim.declared_amount < policy.coupon_check_min_amount:
        return None

    seen: set = ctx.setdefault("_seen_r02", set())
    dedup_key = (claim.employee, claim.order_date)
    if dedup_key in seen:
        return None

    available = ctx.get("coupons") or []
    if not available:
        return None
    used = set(order.coupons_used or [])
    unused = [c for c in available if c not in used]
    if not unused:
        return None

    seen.add(dedup_key)
    return Finding(
        rule_id="R02",
        rule_name="券未核销",
        severity=MINOR,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected=f"应使用可用券 {len(available)} 张",
        actual=f"实际核销 {len(used)} 张，未用 {len(unused)} 张",
        delta=f"{len(unused)}",
        evidence=[
            f"订单号 {claim.order_no}",
            f"未核销券：{', '.join(str(u) for u in unused[:5])}",
            "券来源：MCP query-my-coupons / query-store-coupons",
        ],
        suggestion="纳入报销前置校验：下单前自动匹配可用券，未核销需在申报时说明理由。",
    )


def rule_budget_excess(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    policy: Policy = ctx["policy"]
    amount = claim.declared_amount

    if amount > policy.per_claim_cap:
        cap, scope = policy.per_claim_cap, "单笔"
    elif ctx.get("person_daily", 0.0) > policy.per_person_daily_cap:
        amount, cap, scope = ctx["person_daily"], policy.per_person_daily_cap, "单人单日"
    elif ctx.get("dept_monthly", 0.0) > policy.dept_monthly_cap:
        amount, cap, scope = ctx["dept_monthly"], policy.dept_monthly_cap, "部门单月"
    else:
        return None

    return Finding(
        rule_id="R03",
        rule_name="超预算",
        severity=SIGNIFICANT,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected=f"{scope}上限 {_money(cap)} 元",
        actual=f"{scope}实际 {_money(amount)} 元",
        delta=f"{_money(amount - cap)}",
        evidence=[
            f"订单号 {claim.order_no}",
            f"判定层级：{scope}",
            f"申报金额 {_money(claim.declared_amount)} 元",
        ],
        suggestion="补走超预算审批流程；如无审批，按超标金额不予报销。",
    )


def rule_item_mismatch(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    declared = {str(x).strip() for x in claim.declared_items if str(x).strip()}
    actual = {str(x).strip() for x in order.items if str(x).strip()}
    if not declared or not actual:
        return None
    extra = declared - actual
    if not extra:
        return None
    return Finding(
        rule_id="R04",
        rule_name="串项",
        severity=SIGNIFICANT,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected=f"订单实际构成：{', '.join(sorted(actual))[:80]}",
        actual=f"申报品项：{', '.join(sorted(declared))[:80]}",
        delta=f"{len(extra)}",
        evidence=[
            f"订单号 {claim.order_no}",
            f"申报有、订单无：{', '.join(sorted(extra))[:80]}",
            "套餐构成来源：MCP query-meal-detail",
        ],
        suggestion="更正申报明细；如属非麦当劳消费混入，应剔除出本报销单。",
    )


def rule_duplicate_claim(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    idx: dict[str, list[Claim]] = ctx.get("order_index", {})
    same = idx.get(claim.order_no, [])
    if len(same) <= 1:
        return None
    others = [c.claim_id for c in same if c.claim_id != claim.claim_id]
    return Finding(
        rule_id="R05",
        rule_name="重复报销",
        severity=MAJOR,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected="同一订单仅可申报一次",
        actual=f"该订单号共申报 {len(same)} 次",
        delta=f"{len(same) - 1}",
        evidence=[
            f"订单号 {claim.order_no}",
            f"重复申报单号：{', '.join(others)}",
            f"涉及金额合计 {_money(sum(c.declared_amount for c in same))} 元",
        ],
        suggestion="立即冻结相关付款，逐笔核实后追回；同步检查报销系统是否缺少订单号唯一性校验（系统性控制缺陷）。",
    )


def rule_off_hours(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    policy: Policy = ctx["policy"]
    dt = order.dt
    if dt is None:
        return None
    t = dt.time()
    if policy.allowed_start <= t <= policy.allowed_end:
        return None
    return Finding(
        rule_id="R06",
        rule_name="非工时订单",
        severity=MINOR,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected=f"允许时段 {policy.allowed_start.strftime('%H:%M')}-{policy.allowed_end.strftime('%H:%M')}",
        actual=f"下单时间 {order.order_time}",
        delta="-",
        evidence=[
            f"订单号 {claim.order_no}",
            f"渠道：{order.channel or '未标注'}",
            "若属加班用餐，应附加班审批记录",
        ],
        suggestion="补充加班审批；无审批的，按个人消费处理。",
    )


def rule_late_filing(claim: Claim, order: OrderFact, ctx: dict) -> Finding | None:
    policy: Policy = ctx["policy"]
    od, cd = claim.order_dt, claim.claim_dt
    if not od or not cd:
        return None
    gap = (cd - od).days
    if gap <= policy.filing_window_days:
        return None
    return Finding(
        rule_id="R07",
        rule_name="超期申报",
        severity=MINOR,
        claim_id=claim.claim_id,
        employee=claim.employee,
        dept=claim.dept,
        expected=f"订单发生后 {policy.filing_window_days} 天内申报",
        actual=f"实际间隔 {gap} 天",
        delta=f"{gap - policy.filing_window_days}",
        evidence=[
            f"订单号 {claim.order_no}",
            f"订单日期 {claim.order_date}，申报日期 {claim.claim_date}",
        ],
        suggestion="纳入期间费用截止性测试；跨期申报应调整至实际发生期间。",
    )


RULES: list[tuple[str, Callable[[Claim, OrderFact, dict], Finding | None]]] = [
    ("R01", rule_price_variance),
    ("R02", rule_coupon_unused),
    ("R03", rule_budget_excess),
    ("R04", rule_item_mismatch),
    ("R05", rule_duplicate_claim),
    ("R06", rule_off_hours),
    ("R07", rule_late_filing),
]


# ---------------------------------------------------------------- 主流程

class Auditor:
    def __init__(self, policy: Policy | None = None, verbose: bool = False):
        self.policy = policy or Policy()
        self.verbose = verbose

    def run(
        self,
        claims: list[Claim],
        facts: dict[str, OrderFact],
        enrich: Callable[[Claim, dict], dict] | None = None,
        data_source: str = "sample",
        scope: str = "",
    ) -> AuditReport:
        """
        enrich: 可选回调，用于在逐笔审计时按 claim 取 MCP 实时基准。
                返回 {"benchmark": float|None, "coupons": [...], "meal_detail": ...}
        """
        order_index: dict[str, list[Claim]] = {}
        for c in claims:
            order_index.setdefault(c.order_no, []).append(c)

        person_daily: dict[tuple[str, str], float] = {}
        for c in claims:
            key = (c.employee, c.order_date)
            person_daily[key] = person_daily.get(key, 0.0) + c.declared_amount

        dept_monthly: dict[str, float] = {}
        for c in claims:
            key = c.dept + "|" + c.order_date[:7]
            dept_monthly[key] = dept_monthly.get(key, 0.0) + c.declared_amount

        report = AuditReport(
            scope=scope or f"{len(claims)} 笔申报",
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            policy=self.policy,
            claims_total=len(claims),
            claims_amount=round(sum(c.declared_amount for c in claims), 2),
            data_source=data_source,
        )

        for c in claims:
            order = facts.get(c.order_no)
            if order is None:
                report.unmatched.append(c.claim_id)
                order = OrderFact(order_no=c.order_no, source="missing")

            ctx: dict[str, Any] = {
                "policy": self.policy,
                "order_index": order_index,
                "person_daily": person_daily.get((c.employee, c.order_date), 0.0),
                "dept_monthly": dept_monthly.get(c.dept + "|" + c.order_date[:7], 0.0),
            }
            if enrich:
                try:
                    ctx.update(enrich(c, ctx) or {})
                except Exception as e:  # 取数失败不阻断，降级为离线基准
                    if self.verbose:
                        print(f"[warn] {c.claim_id} 取数失败：{e}")

            for rule_id, fn in RULES:
                f = fn(c, order, ctx)
                if f:
                    report.findings.append(f)

        report.findings.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.rule_id))
        return report


# ---------------------------------------------------------------- 输入 / 输出

def load_claims_csv(path: str | Path) -> list[Claim]:
    """申报表 CSV。列：claim_id,employee,dept,order_no,order_date,claim_date,declared_amount,declared_items,purpose,meal_type"""
    claims: list[Claim] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            items = [x.strip() for x in (row.get("declared_items") or "").replace("|", ",").split(",") if x.strip()]
            claims.append(
                Claim(
                    claim_id=(row.get("claim_id") or "").strip(),
                    employee=(row.get("employee") or "").strip(),
                    dept=(row.get("dept") or "").strip(),
                    order_no=(row.get("order_no") or "").strip(),
                    order_date=(row.get("order_date") or "").strip(),
                    claim_date=(row.get("claim_date") or "").strip(),
                    declared_amount=float(row.get("declared_amount") or 0),
                    declared_items=items,
                    purpose=(row.get("purpose") or "").strip(),
                    meal_type=(row.get("meal_type") or "个人").strip(),
                )
            )
    return claims


def report_to_dict(report: AuditReport) -> dict:
    return {
        "scope": report.scope,
        "generated_at": report.generated_at,
        "data_source": report.data_source,
        "claims_total": report.claims_total,
        "claims_amount": report.claims_amount,
        "exposure": report.exposure,
        "policy": asdict(report.policy) | {
            "allowed_start": report.policy.allowed_start.strftime("%H:%M"),
            "allowed_end": report.policy.allowed_end.strftime("%H:%M"),
        },
        "unmatched_claims": report.unmatched,
        "findings": [asdict(f) for f in report.findings],
    }


def render_markdown(report: AuditReport) -> str:
    p = report.policy
    sev = report.by_severity
    L: list[str] = []

    L.append("# 订餐报销稽核底稿\n")
    L.append(f"- 审计范围：{report.scope}")
    L.append(f"- 生成时间：{report.generated_at}")
    L.append(f"- 数据来源：**{report.data_source}**（mcp=实时调用 / sample=脱敏样例）")
    L.append(f"- 申报笔数：{report.claims_total}　申报总额：{_money(report.claims_amount)} 元")
    L.append(f"- 缺陷合计：**{len(report.findings)}** 条 "
             f"（{MAJOR} {len(sev[MAJOR])} / {SIGNIFICANT} {len(sev[SIGNIFICANT])} / {MINOR} {len(sev[MINOR])}）")
    L.append(f"- 可量化敞口：**{_money(report.exposure)} 元**\n")

    L.append("## 一、控制点台账\n")
    L.append("| 编号 | 控制点 | 控制目标 | 缺陷分级 | 取证方式 |")
    L.append("|---|---|---|---|---|")
    for rid, name, target, level, method in CONTROL_POINTS:
        L.append(f"| {rid} | {name} | {target} | {level} | {method} |")
    L.append("")

    L.append("## 二、缺陷清单\n")
    if not report.findings:
        L.append("未发现异常。\n")
    else:
        L.append("| 编号 | 规则 | 分级 | 申报单 | 人员/部门 | 应然 | 实然 | 差异 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for f in report.findings:
            L.append(f"| {f.rule_id} | {f.rule_name} | **{f.severity}** | {f.claim_id} | "
                     f"{f.employee}／{f.dept} | {f.expected} | {f.actual} | {f.delta} |")
        L.append("")

        L.append("## 三、逐笔证据链\n")
        for f in report.findings:
            L.append(f"### {f.rule_id} · {f.rule_name}（{f.severity}）· {f.claim_id}\n")
            L.append(f"- 应然：{f.expected}")
            L.append(f"- 实然：{f.actual}")
            for e in f.evidence:
                L.append(f"- 证据：{e}")
            L.append(f"- **整改建议**：{f.suggestion}\n")

    L.append("## 四、结论与局限\n")
    L.append(f"1. 本次共执行 {len(RULES)} 项控制测试，覆盖 {report.claims_total} 笔申报。")
    L.append(f"2. {MAJOR}缺陷 {len(sev[MAJOR])} 条，涉及费用真实性，应优先处理并追溯系统性控制缺失。")
    L.append(f"3. {SIGNIFICANT}缺陷 {len(sev[SIGNIFICANT])} 条，涉及金额准确性，需补充凭证后复核。")
    if report.unmatched:
        L.append(f"4. **{len(report.unmatched)} 笔申报未取到订单事实**（{', '.join(report.unmatched[:10])}），"
                 "已按「无法取证」处理，不构成无缺陷结论。")
    L.append("5. 本底稿为程序化测试结果，不等同于审计意见；最终结论须经人工复核并签字确认。")
    L.append("")
    L.append("---")
    L.append(f"_由 mcd-audit-officer 生成 · 政策阈值：单笔 {p.per_claim_cap} 元 / "
             f"单人单日 {p.per_person_daily_cap} 元 / 部门单月 {p.dept_monthly_cap} 元 / "
             f"申报时限 {p.filing_window_days} 天_")

    return "\n".join(L)
