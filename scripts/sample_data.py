#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
脱敏样例数据。

全部为虚构数据，不涉及任何真实订单号、真实人名或真实门店。
用途：无 MCP Token 时也能完整跑通稽核流程，保证结论可离线复核——
这也是内控审计对"可重放性"的基本要求。

样例刻意覆盖全部 7 条稽核规则，便于一眼看懂每个控制点怎么命中。
"""

from __future__ import annotations

import csv
import io

from audit_engine import Claim, OrderFact

# 虚构申报人（非真实自然人）
CLAIMS_CSV = """claim_id,employee,dept,order_no,order_date,claim_date,declared_amount,declared_items,purpose,meal_type
C-001,员工A,研发部,MCD-SAMPLE-0001,2026-09-02,2026-09-05,42.50,巨无霸|中薯条|中可乐,项目攻坚加班用餐,个人
C-002,员工B,研发部,MCD-SAMPLE-0002,2026-09-02,2026-09-06,38.00,麦辣鸡腿堡|中可乐,项目攻坚加班用餐,个人
C-003,员工C,市场部,MCD-SAMPLE-0003,2026-09-03,2026-09-08,128.00,板烧鸡腿堡|麦香鱼|中薯条|中可乐|麦乐鸡,客户拜访工作餐,团餐
C-004,员工A,研发部,MCD-SAMPLE-0001,2026-09-02,2026-09-20,42.50,巨无霸|中薯条|中可乐,项目攻坚加班用餐,个人
C-005,员工D,运营部,MCD-SAMPLE-0004,2026-09-05,2026-09-09,26.00,吉士蛋麦满分|热豆浆,早会备餐,个人
C-006,员工E,运营部,MCD-SAMPLE-0005,2026-09-08,2026-09-10,95.00,双层吉士汉堡|麦辣鸡翅|大薯条|大可乐,月度复盘会,团餐
C-007,员工F,市场部,MCD-SAMPLE-0006,2026-09-10,2026-11-25,58.00,麦香鸡|菠萝派|中可乐,展会值守,个人
C-008,员工G,研发部,MCD-SAMPLE-0007,2026-09-12,2026-09-15,33.00,1+1随心配|中可乐,日常加班,个人
C-009,员工H,财务部,MCD-SAMPLE-0008,2026-09-15,2026-09-18,76.00,安格斯厚牛堡|中薯条|零度可乐,季度结账加班,个人
C-010,员工I,市场部,MCD-SAMPLE-0009,2026-09-18,2026-09-22,45.00,麦辣鸡腿堡|麦辣鸡翅|中可乐,客户陪同用餐,团餐
C-011,员工J,运营部,MCD-SAMPLE-0010,2026-09-20,2026-09-24,29.00,火腿扒麦满分|热咖啡,巡检外勤早餐,个人
C-012,员工K,研发部,MCD-SAMPLE-0011,2026-09-22,2026-09-25,88.00,巨无霸|麦香鸡|中薯条|中可乐|苹果派,通宵发版,个人
"""

# 订单事实（对应上面的订单号）。虚构门店，非真实门店编码。
FACTS: dict[str, OrderFact] = {
    "MCD-SAMPLE-0001": OrderFact(
        order_no="MCD-SAMPLE-0001", store_id="ST-DEMO-01", store_name="（样例）示范餐厅",
        order_time="2026-09-02 12:18", paid_amount=42.50,
        items=["巨无霸", "中薯条", "中可乐"], coupons_used=["优惠券-DEMO-A"],
        channel="麦乐送", source="sample",
    ),
    "MCD-SAMPLE-0002": OrderFact(
        order_no="MCD-SAMPLE-0002", store_id="ST-DEMO-01", store_name="（样例）示范餐厅",
        order_time="2026-09-02 12:25", paid_amount=38.00,
        items=["麦辣鸡腿堡", "中可乐"], coupons_used=[],
        channel="麦乐送", source="sample",
    ),
    "MCD-SAMPLE-0003": OrderFact(
        order_no="MCD-SAMPLE-0003", store_id="ST-DEMO-02", store_name="（样例）园区餐厅",
        order_time="2026-09-03 18:40", paid_amount=112.00,
        items=["板烧鸡腿堡", "麦香鱼", "中薯条", "中可乐"], coupons_used=["优惠券-DEMO-B"],
        channel="到店取餐", source="sample",
    ),
    "MCD-SAMPLE-0004": OrderFact(
        order_no="MCD-SAMPLE-0004", store_id="ST-DEMO-01", store_name="（样例）示范餐厅",
        order_time="2026-09-05 08:05", paid_amount=26.00,
        items=["吉士蛋麦满分", "热豆浆"], coupons_used=["优惠券-DEMO-C"],
        channel="到店取餐", source="sample",
    ),
    "MCD-SAMPLE-0005": OrderFact(
        order_no="MCD-SAMPLE-0005", store_id="ST-DEMO-02", store_name="（样例）园区餐厅",
        order_time="2026-09-08 19:10", paid_amount=95.00,
        items=["双层吉士汉堡", "麦辣鸡翅", "大薯条", "大可乐"], coupons_used=[],
        channel="团餐", source="sample",
    ),
    "MCD-SAMPLE-0006": OrderFact(
        order_no="MCD-SAMPLE-0006", store_id="ST-DEMO-03", store_name="（样例）会展餐厅",
        order_time="2026-09-10 23:35", paid_amount=58.00,
        items=["麦香鸡", "菠萝派", "中可乐"], coupons_used=[],
        channel="麦乐送", source="sample",
    ),
    "MCD-SAMPLE-0007": OrderFact(
        order_no="MCD-SAMPLE-0007", store_id="ST-DEMO-01", store_name="（样例）示范餐厅",
        order_time="2026-09-12 20:55", paid_amount=31.00,
        items=["1+1随心配", "中可乐"], coupons_used=["优惠券-DEMO-D"],
        channel="麦乐送", source="sample",
    ),
    "MCD-SAMPLE-0008": OrderFact(
        order_no="MCD-SAMPLE-0008", store_id="ST-DEMO-02", store_name="（样例）园区餐厅",
        order_time="2026-09-15 20:12", paid_amount=64.00,
        items=["安格斯厚牛堡", "中薯条", "零度可乐"], coupons_used=[],
        channel="麦乐送", source="sample",
    ),
    "MCD-SAMPLE-0009": OrderFact(
        order_no="MCD-SAMPLE-0009", store_id="ST-DEMO-03", store_name="（样例）会展餐厅",
        order_time="2026-09-18 13:05", paid_amount=45.00,
        items=["麦辣鸡腿堡", "麦辣鸡翅", "中可乐"], coupons_used=["优惠券-DEMO-E"],
        channel="到店取餐", source="sample",
    ),
    "MCD-SAMPLE-0010": OrderFact(
        order_no="MCD-SAMPLE-0010", store_id="ST-DEMO-01", store_name="（样例）示范餐厅",
        order_time="2026-09-20 07:48", paid_amount=29.00,
        items=["火腿扒麦满分", "热咖啡"], coupons_used=["优惠券-DEMO-F"],
        channel="到店取餐", source="sample",
    ),
    "MCD-SAMPLE-0011": OrderFact(
        order_no="MCD-SAMPLE-0011", store_id="ST-DEMO-02", store_name="（样例）园区餐厅",
        order_time="2026-09-22 22:40", paid_amount=72.00,
        items=["巨无霸", "麦香鸡", "中薯条", "中可乐"], coupons_used=[],
        channel="麦乐送", source="sample",
    ),
}

# 样例：账户下存在但未在部分订单中核销的券（虚构）
AVAILABLE_COUPONS = ["优惠券-DEMO-B", "优惠券-DEMO-G", "优惠券-DEMO-H"]


def load_sample_claims() -> list[Claim]:
    """从内嵌 CSV 文本解析申报数据，无需外部文件。"""
    rows = list(csv.DictReader(io.StringIO(CLAIMS_CSV.strip())))
    return [
        Claim(
            claim_id=r["claim_id"].strip(),
            employee=r["employee"].strip(),
            dept=r["dept"].strip(),
            order_no=r["order_no"].strip(),
            order_date=r["order_date"].strip(),
            claim_date=r["claim_date"].strip(),
            declared_amount=float(r["declared_amount"]),
            declared_items=[x.strip() for x in r["declared_items"].split("|") if x.strip()],
            purpose=r["purpose"].strip(),
            meal_type=r["meal_type"].strip(),
        )
        for r in rows
    ]


def sample_facts() -> dict[str, OrderFact]:
    return dict(FACTS)
