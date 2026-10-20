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
from mcp_client import to_yuan  # noqa: E402
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

    menu_cache: dict[str, dict[str, str]] = {}
    store_by_order: dict[str, str] | None = None

    def _store_lookup() -> dict[str, str]:
        """
        query-order **不返回 storeCode**（只有 storeName / storeAddress），
        而 calculate-price 必须要 storeCode。所以先用 order-list 建
        「orderId -> storeCode」索引来补这一环。

        这是实测才发现的坑：不补这一步，核价整条链路会被静默跳过，
        底稿上看不出任何异常，只是 R01 永远不命中。
        """
        nonlocal store_by_order
        if store_by_order is None:
            store_by_order = {}
            payload = _pick_data(client.list_orders())
            rows = (payload or {}).get("list") if isinstance(payload, dict) else None
            for row in rows or []:
                oid = row.get("orderId")
                sc = row.get("storeCode")
                if oid and sc:
                    store_by_order[str(oid)] = str(sc)
            if verbose:
                print(f"[mcp] order-list 索引 {len(store_by_order)} 笔", file=sys.stderr)
        return store_by_order

    def _index_for(store_code: str, order_type: int, be_type: int) -> dict[str, str]:
        """按门店缓存「品项名 -> productCode」索引，避免每笔申报都重拉菜单。"""
        key = f"{store_code}:{order_type}:{be_type}"
        if key not in menu_cache:
            menu_cache[key] = _menu_index(
                client.query_meals(store_code, order_type=order_type, be_type=be_type)
            )
        return menu_cache[key]

    def enrich(claim, ctx) -> dict:
        out: dict = {}
        if claim.order_no in cache:
            return cache[claim.order_no]

        facts = ctx.get("_facts", {})
        order = facts.get(claim.order_no)
        raw_order: dict | None = None

        # 订单事实优先取 MCP（query-order 用 orderId，即订单编码）
        try:
            resp = client.fetch_order(claim.order_no)
            raw_order = _pick_data(resp)
            if isinstance(raw_order, dict) and raw_order:
                order = _coerce_order(claim.order_no, raw_order, order)
                if not order.store_id:
                    order.store_id = _store_lookup().get(str(claim.order_no), "")
                    if verbose and not order.store_id:
                        print(
                            f"[warn] {claim.order_no} 无法从 order-list 补到 storeCode，核价将跳过",
                            file=sys.stderr,
                        )
                facts[claim.order_no] = order
        except McpError as e:
            if verbose:
                print(f"[warn] {claim.order_no} 取订单失败：{e}", file=sys.stderr)

        # 官方核价（calculate-price；beType/orderType 以订单实际渠道为准）
        if order and order.store_id:
            try:
                ot = _as_int((raw_order or {}).get("orderType"), 1)
                bt = _as_int((raw_order or {}).get("beType"), 1)
                idx = _index_for(order.store_id, ot, bt)
                items, missing = _items_payload(list(order.items or []), idx)
                if missing:
                    # 基准残缺：宁可不出价差结论，也不拿残缺基准造缺陷。
                    out["benchmark_incomplete"] = missing
                    if verbose:
                        print(
                            f"[warn] {claim.order_no} 有 {len(missing)} 个品项未映射到 productCode"
                            f"（{missing[:3]}），核价基准不完整 → 放弃价差判定",
                            file=sys.stderr,
                        )
                elif items:
                    bp = client.benchmark_price(
                        order.store_id, items, order_type=ot, be_type=bt
                    )
                    # ⚠️ calculate-price 金额单位是「分」
                    amount = _dig_amount(_pick_data(bp), unit="cent")
                    if amount is not None:
                        out["benchmark"] = amount
                        if verbose:
                            print(
                                f"[mcp] {claim.order_no} 核价 {amount:.2f} 元 "
                                f"（items={len(items)}，store={order.store_id}）",
                                file=sys.stderr,
                            )
                    elif verbose:
                        print(f"[mcp] {claim.order_no} 核价未取到金额", file=sys.stderr)
            except McpError:
                pass

        # 可用券
        try:
            coupons = client.my_coupons()
            if isinstance(coupons, (list, dict)):
                ids = _dig_coupon_ids(_pick_data(coupons))
                if ids:
                    out["coupons"] = ids
        except McpError:
            pass

        cache[claim.order_no] = out
        return out

    return enrich


def _pick_data(payload):
    """官方响应形如 {code, message, data}，这里把 data 层剥出来。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        return payload["data"]
    return payload


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default



def _norm_name(s) -> str:
    """品项名归一化：去空白、统一全半角括号、去间隔号，提升菜单匹配率。"""
    s = str(s).strip().lower()
    for a, b in (("（", "("), ("）", ")"), ("　", ""), (" ", ""), ("·", ""), ("•", "")):
        s = s.replace(a, b)
    return s


def _items_payload(items: list[str], index: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """
    把品项名转成 calculate-price 的入参：[{"productCode": "9900005456", "quantity": 1}]。

    真实接口只认 productCode，不认品项名，所以必须先用 query-meals 建索引。
    返回 (可核价品项, 未能映射的品项名)。

    ⚠️ 只要有品项映射不上，核价基准就是残缺的 —— 调用方**必须**放弃价差判定，
    否则会拿「少算了几样的价格」去对「完整订单金额」，凭空造出假价差。
    """
    idx = index or {}
    out: list[dict] = []
    missing: list[str] = []
    for it in items:
        code = idx.get(_norm_name(it)) or idx.get(it)
        if code:
            out.append({"productCode": code, "quantity": 1})
        else:
            missing.append(it)
    return out, missing


def _menu_index(payload) -> dict[str, str]:
    """从 query-meals 返回里建「品项名 -> productCode」索引（含归一化键）。

    实测 data.meals 是 {productCode: {name, currentPrice, originalPrice}} 映射。
    """
    idx: dict[str, str] = {}
    if not isinstance(payload, dict):
        return idx
    data = payload.get("data") if "data" in payload else payload
    meals = (data or {}).get("meals") if isinstance(data, dict) else None
    if isinstance(meals, dict):
        for code, info in meals.items():
            name = (info or {}).get("name") if isinstance(info, dict) else None
            if name and code:
                idx[str(name)] = str(code)
                idx[_norm_name(name)] = str(code)
    elif isinstance(meals, list):
        for m in meals:
            if isinstance(m, dict) and m.get("productCode") and m.get("name"):
                idx[str(m["name"])] = str(m["productCode"])
                idx[_norm_name(m["name"])] = str(m["productCode"])
    return idx



def _dig_amount(payload, unit: str = "yuan") -> float | None:
    """
    从 MCP 返回里抠出金额，统一成「元」。

    unit 必须由调用方指定：订单接口是「元」，calculate-price 是「分」。
    自动猜测单位在这个场景里是危险的 —— 猜错会让 112.4 变成 11240。
    """
    if payload is None:
        return None
    if isinstance(payload, (int, float, str)):
        return to_yuan(payload, unit)
    if isinstance(payload, dict):
        for key in ("payAmount", "realTotalAmount", "totalAmount", "price", "amount", "payable", "total"):
            v = payload.get(key)
            if isinstance(v, (int, float, str)) and not isinstance(v, bool):
                got = to_yuan(v, unit)
                if got is not None:
                    return got
        for v in payload.values():
            got = _dig_amount(v, unit)
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
    """
    把 query-order 的真实返回收敛到 OrderFact。

    实测字段（2026-10-20 对 mcp.mcd.cn）：
      orderId / storeCode / storeName / createTime
      / realTotalAmount（单位「元」）/ totalDiscountAmount
      / orderProductList[{productCode, productName, quantity, comboItemList}]
      / couponList / takeWay
    """
    base = fallback or OrderFact(order_no=order_no, source="mcp")
    paid = _dig_amount(raw, unit="yuan")  # 订单侧金额是「元」

    names: list[str] = []
    for p in raw.get("orderProductList") or []:
        if isinstance(p, dict) and p.get("productName"):
            names.append(str(p["productName"]))

    coupons: list[str] = []
    for cc in raw.get("couponList") or []:
        if isinstance(cc, dict):
            val = cc.get("code") or cc.get("couponCode") or cc.get("name")
            if val:
                coupons.append(str(val))
        elif cc:
            coupons.append(str(cc))

    return OrderFact(
        order_no=order_no,
        store_id=str(raw.get("storeCode") or raw.get("storeId") or base.store_id or ""),
        store_name=str(raw.get("storeName") or base.store_name or ""),
        order_time=str(raw.get("createTime") or raw.get("orderTime") or base.order_time or ""),
        paid_amount=paid if paid is not None else base.paid_amount,
        items=names or base.items or [],
        coupons_used=coupons or base.coupons_used or [],
        channel=str(raw.get("takeWay") or raw.get("orderType") or base.channel or ""),
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
            # 真实模式下**绝不注入示例券**。
            # 早期版本在这里 setdefault 了 AVAILABLE_COUPONS，会让 R02 拿演示券当真实证据，
            # 与「降级必须可见、不给虚假结论」的原则直接冲突，已移除。

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
