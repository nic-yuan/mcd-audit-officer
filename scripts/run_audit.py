#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mcd-audit-officer · 命令行入口

    # 1) 离线演示（无需任何配置，clone 即可跑）
    python scripts/run_audit.py

    # 2) 用你自己的申报表
    python scripts/run_audit.py --csv examples/expense_sample.csv

    # 3) 接入麦当劳 MCP 实时数据（需先配置 Token）
    export MCD_MCP_TOKEN=你的Token
    python scripts/run_audit.py --csv my_claims.csv --source mcp

    # 4) 自检 MCP 连通性
    python scripts/run_audit.py --probe
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_engine import Auditor, Policy, OrderFact, load_claims_csv, render_markdown, report_to_dict  # noqa: E402
from sample_data import load_sample_claims, sample_facts, AVAILABLE_COUPONS  # noqa: E402


def build_mcp_enricher(verbose: bool = False):
    """
    构造 MCP 取数回调：为每一笔申报补充
      - benchmark：calculate-price 官方核价（价差的"应然基准"）
      - coupons：账户可用券（券未核销核查）
    任何失败都返回空 dict，引擎自动降级为离线基准。
    """
    from mcp_client import McdMcpClient, McpError

    client = McdMcpClient()
    cache: dict[str, dict] = {}

    try:
        client.initialize()
    except McpError as e:
        print(f"[warn] MCP 初始化失败，全程降级为离线基准：{e}", file=sys.stderr)
        return None

    def enrich(claim, ctx) -> dict:
        out: dict = {}
        if claim.order_no in cache:
            return cache[claim.order_no]

        facts = ctx.get("_facts", {})
        order = facts.get(claim.order_no)

        # 订单事实优先取 MCP
        try:
            raw = client.fetch_order(claim.order_no)
            if isinstance(raw, dict) and raw:
                order = _coerce_order(claim.order_no, raw, order)
                facts[claim.order_no] = order
        except McpError as e:
            if verbose:
                print(f"[warn] {claim.order_no} 取订单失败：{e}", file=sys.stderr)

        # 官方核价
        if order and order.store_id:
            try:
                bp = client.benchmark_price(order.store_id, _items_payload(order.items))
                amount = _dig_amount(bp)
                if amount is not None:
                    out["benchmark"] = amount
            except McpError:
                pass

        # 可用券
        try:
            coupons = client.my_coupons()
            if isinstance(coupons, (list, dict)):
                ids = _dig_coupon_ids(coupons)
                if ids:
                    out["coupons"] = ids
        except McpError:
            pass

        cache[claim.order_no] = out
        return out

    return enrich


def _items_payload(items: list[str]) -> list[dict]:
    """把品项名转成核价入参。真实使用时应按餐品编码传参，此处仅作演示。"""
    return [{"name": it} for it in items]


def _dig_amount(payload) -> float | None:
    """从 MCP 返回里尽量抠出金额字段，兼容不同层级结构。"""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        for key in ("payAmount", "totalAmount", "amount", "payable", "total", "price"):
            v = payload.get(key)
            if isinstance(v, (int, float)):
                return float(v)
        for v in payload.values():
            got = _dig_amount(v)
            if got is not None:
                return got
    return None


def _dig_coupon_ids(payload) -> list[str]:
    ids: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                for key in ("couponId", "id", "name"):
                    if item.get(key):
                        ids.append(str(item[key]))
                        break
            elif item:
                ids.append(str(item))
    elif isinstance(payload, dict):
        for key in ("coupons", "list", "items", "data"):
            if isinstance(payload.get(key), list):
                return _dig_coupon_ids(payload[key])
    return ids


def _coerce_order(order_no: str, raw: dict, fallback: OrderFact | None) -> OrderFact:
    """把 MCP 返回的订单结构收敛到 OrderFact。字段缺失时保留样例值。"""
    base = fallback or OrderFact(order_no=order_no, source="mcp")
    paid = _dig_amount(raw)
    return OrderFact(
        order_no=order_no,
        store_id=str(raw.get("storeId") or raw.get("storeCode") or base.store_id or ""),
        store_name=str(raw.get("storeName") or base.store_name or ""),
        order_time=str(raw.get("orderTime") or raw.get("createTime") or base.order_time or ""),
        paid_amount=paid if paid is not None else base.paid_amount,
        items=raw.get("items") or raw.get("mealList") or base.items or [],
        coupons_used=raw.get("coupons") or base.coupons_used or [],
        channel=str(raw.get("channel") or raw.get("orderType") or base.channel or ""),
        source="mcp",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="麦审官 · 订餐报销稽核")
    ap.add_argument("--csv", help="申报表 CSV 路径；缺省使用内置脱敏样例")
    ap.add_argument("--policy", help="政策阈值 JSON 文件路径")
    ap.add_argument("--source", choices=["sample", "mcp"], default="sample", help="订单事实来源")
    ap.add_argument("--out", help="输出底稿路径（Markdown）；缺省打印到标准输出")
    ap.add_argument("--json", dest="json_out", help="额外输出 JSON 结果路径")
    ap.add_argument("--probe", action="store_true", help="仅自检 MCP 连通性后退出")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.probe:
        from mcp_client import probe
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
        return 0

    # 1) 输入
    if args.csv:
        claims = load_claims_csv(args.csv)
        facts: dict = {}
    else:
        claims = load_sample_claims()
        facts = sample_facts()

    # 2) 政策
    policy = Policy()
    if args.policy:
        policy = Policy.from_dict(json.loads(Path(args.policy).read_text(encoding="utf-8")))

    # 3) 取数增强
    enrich = None
    data_source = "sample"
    if args.source == "mcp":
        if not os.environ.get("MCD_MCP_TOKEN"):
            print("[error] 未设置 MCD_MCP_TOKEN，无法使用 --source mcp", file=sys.stderr)
            return 2
        enrich = build_mcp_enricher(verbose=args.verbose)
        if enrich is None:
            print("[warn] MCP 不可用，本次按离线基准执行", file=sys.stderr)
        else:
            data_source = "mcp"
            # 离线样例场景下补充券信息，便于演示 R02
            def _with_default_coupons(c, ctx, _inner=enrich):
                out = _inner(c, ctx) or {}
                out.setdefault("coupons", AVAILABLE_COUPONS)
                return out
            enrich = _with_default_coupons

    # 4) 执行
    if args.csv:
        if args.source == "sample" and not facts:
            print(
                "[warn] 自定义申报表 + 离线模式：无订单事实可供比对，\n"
                "       所有申报将计入「无法取证」，不会判定任何缺陷（不会给出虚假的干净结论）。\n"
                "       要真正稽核，请加 --source mcp 并提供 MCD_MCP_TOKEN。",
                file=sys.stderr,
            )
        auditor = Auditor(policy=policy, verbose=args.verbose)
        report = auditor.run(claims, facts, enrich=enrich, data_source=data_source)
    else:
        auditor = Auditor(policy=policy, verbose=args.verbose)
        if enrich is not None:
            original = enrich

            def _wrapped(c, ctx, _o=original, _f=facts):
                ctx["_facts"] = _f
                return _o(c, ctx)
            enrich = _wrapped
        else:
            # 离线演示：注入样例券，使 R02 券未核销可见（真实场景由 MCP 取数）
            def enrich(c, ctx):  # noqa: F811
                return {"coupons": AVAILABLE_COUPONS}
        report = auditor.run(claims, facts, enrich=enrich, data_source=data_source)

    # 5) 输出
    md = render_markdown(report)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[ok] 底稿已写入 {args.out}")
    else:
        print(md)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[ok] JSON 已写入 {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
