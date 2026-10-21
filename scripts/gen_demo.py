#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成 demo.html：把一次稽核结果渲染成单页静态报告。

无外部依赖、无 CDN、无 JS 框架，双击即可打开。
数据来自内置脱敏样例，可离线复现。

    python scripts/gen_demo.py            # 输出 demo.html
    python scripts/gen_demo.py --out x.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_engine import Auditor, render_markdown, report_to_dict  # noqa: E402
from sample_data import load_sample_claims, sample_facts, AVAILABLE_COUPONS  # noqa: E402

CSS = """
:root{
  --bg:#f7f8fa; --card:#ffffff; --line:#e3e6ec; --ink:#1b1f27; --muted:#6b7280;
  --major:#c0392b; --sig:#d68910; --minor:#5d6d7e; --accent:#1f6feb; --ok:#1e8449;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.7;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:40px 24px 64px}
h1{font-size:28px;margin:0 0 8px;letter-spacing:-.3px}
.sub{color:var(--muted);margin:0 0 4px;font-size:15px}
.badge{display:inline-block;background:#eef4ff;color:var(--accent);
  border:1px solid #cfe0ff;border-radius:999px;padding:3px 12px;font-size:12px;margin-right:8px}
.flow{display:flex;gap:12px;align-items:stretch;margin:24px 0;flex-wrap:wrap}
.fnode{flex:1;min-width:200px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:16px}
.fnode .k{font-size:12px;color:var(--muted);margin-bottom:4px}
.fnode .v{font-size:16px;font-weight:600}
.fnode .note{font-size:12px;color:var(--muted);margin-top:6px}
.op{align-self:center;color:var(--muted);font-size:20px}
.result{flex:1;min-width:200px;background:linear-gradient(135deg,#fff7ed,#fef9f5);
  border:1px solid #f5d9b8;border-radius:12px;padding:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .k{font-size:12px;color:var(--muted)}
.card .v{font-size:24px;font-weight:700;letter-spacing:-.5px;margin-top:2px}
.card .u{font-size:12px;color:var(--muted);font-weight:400}
h2{font-size:19px;margin:36px 0 12px;padding-bottom:8px;border-bottom:2px solid var(--line)}
table{width:100%;border-collapse:collapse;background:var(--card);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}
th{background:#f1f3f7;text-align:left;padding:10px 12px;font-weight:600;font-size:13px}
td{padding:10px 12px;border-top:1px solid var(--line);vertical-align:top}
tr:hover td{background:#fafbfc}
.sev{font-weight:700;font-size:12px;padding:2px 8px;border-radius:6px;white-space:nowrap}
.sev-重大{background:#fdecea;color:var(--major)}
.sev-重要{background:#fef6e7;color:var(--sig)}
.sev-一般{background:#eef1f4;color:var(--minor)}
.finding{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin-bottom:12px}
.finding.重大{border-left:4px solid var(--major)}
.finding.重要{border-left:4px solid var(--sig)}
.finding.一般{border-left:4px solid var(--minor)}
.finding h3{margin:0 0 10px;font-size:15px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.finding .meta{font-size:12px;color:var(--muted);font-weight:400}
.kv{display:grid;grid-template-columns:80px 1fr;gap:6px 12px;font-size:14px;margin:10px 0}
.kv .k{color:var(--muted)}
.ev{margin:10px 0 0;padding-left:18px;font-size:13px;color:#444}
.ev li{margin:3px 0}
.sug{margin-top:12px;padding:10px 12px;background:#f4f8ff;border-left:3px solid var(--accent);
  border-radius:6px;font-size:13px}
.note-box{background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:16px;font-size:14px}
.note-box ul{margin:8px 0 0;padding-left:20px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
code{background:#f1f3f7;padding:1px 6px;border-radius:4px;font-size:13px}
.cta{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:linear-gradient(135deg,#eef4ff,#f7f9ff);
  border:1px solid #cfe0ff;border-radius:12px;padding:12px 16px;margin:16px 0 8px;font-size:14px}
.cta .txt{flex:1;min-width:220px;color:#1b1f27}
.cta .txt b{color:var(--accent)}
.cta a{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;
  border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;white-space:nowrap}
.cta a.secondary{background:#fff;color:var(--accent);border:1px solid #cfe0ff}
.cta-foot{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin-top:28px;font-size:14px}
"""


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def build(data: dict) -> str:
    p = data["policy"]
    sev_count = {"重大": 0, "重要": 0, "一般": 0}
    for f in data["findings"]:
        sev_count[f["severity"]] = sev_count.get(f["severity"], 0) + 1

    H = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>麦审官 · 订餐报销稽核底稿（样例）</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        "<h1>麦审官 · 订餐报销稽核底稿</h1>",
        "<p class='sub'>基于麦当劳 MCP 的企业团餐与订餐报销稽核 Skill　·　"
        "<b>脱敏样例数据</b>，不含任何真实订单、真实人名或真实门店</p>",
        "<div class='cta'>"
        "<div class='txt'>这是开源项目 <b>麦审官 mcd-audit-officer</b> 的样例输出。"
        "它把「申报单 × 订单事实 × 官方核价」做三单交叉，跑 7 条控制测试，"
        "输出带证据链与缺陷分级的稽核底稿。</div>"
        "<a href='https://github.com/nic-yuan/mcd-audit-officer' target='_blank' rel='noopener'>"
        "查看源码 · Star</a>"
        "<a class='secondary' href='https://github.com/nic-yuan/mcd-audit-officer#readme' "
        "target='_blank' rel='noopener'>使用说明</a>"
        "</div>",
        "<p><span class='badge'>mcd-audit-officer</span>"
        "<span class='badge'>MCP × 内控审计</span>"
        "<span class='badge'>零第三方依赖</span></p>",
        "<div class='flow'>",
        "<div class='fnode'><div class='k'>应然</div><div class='v'>申报单</div>"
        "<div class='note'>报销人自述：金额、品项、事由</div></div>",
        "<div class='op'>×</div>",
        "<div class='fnode'><div class='k'>实然</div><div class='v'>订单事实</div>"
        "<div class='note'>query-order / order-list</div></div>",
        "<div class='op'>×</div>",
        "<div class='fnode'><div class='k'>基准</div><div class='v'>官方核价</div>"
        "<div class='note'>calculate-price · 独立于报销人</div></div>",
        "<div class='op'>=</div>",
        "<div class='result'><div class='k' style='color:var(--muted);font-size:12px'>产出</div>"
        "<div class='v' style='font-size:16px;font-weight:600'>带证据链的稽核底稿</div>"
        "<div class='note'>7 项控制测试 · 三级缺陷分级</div></div>",
        "</div>",
        "<div class='cards'>",
        f"<div class='card'><div class='k'>申报笔数</div><div class='v'>{data['claims_total']}</div></div>",
        f"<div class='card'><div class='k'>申报总额</div><div class='v'>{data['claims_amount']:.2f}<span class='u'> 元</span></div></div>",
        f"<div class='card'><div class='k'>缺陷合计</div><div class='v'>{len(data['findings'])}<span class='u'> 条</span></div></div>",
        f"<div class='card'><div class='k'>重大 / 重要 / 一般</div><div class='v' style='font-size:20px'>"
        f"<span style='color:var(--major)'>{sev_count['重大']}</span> / "
        f"<span style='color:var(--sig)'>{sev_count['重要']}</span> / "
        f"<span style='color:var(--minor)'>{sev_count['一般']}</span></div></div>",
        f"<div class='card'><div class='k'>可量化敞口</div><div class='v' style='color:var(--major)'>"
        f"{data['exposure']:.2f}<span class='u'> 元</span></div></div>",
        "</div>",
    ]

    H.append("<h2>一、控制点台账</h2><table><thead><tr><th>编号</th><th>控制点</th>"
             "<th>控制目标</th><th>分级</th><th>取证方式</th></tr></thead><tbody>")
    for rid, name, target, level, method in [
        ("R01", "价差异常", "申报金额与官方核价一致", "重要", "calculate-price 重算应付总额"),
        ("R02", "券未核销", "可用券应充分使用", "一般", "query-my-coupons 比对已核销券"),
        ("R03", "超预算", "单笔/单人单日/部门单月不超标准", "重要", "三级阈值汇总比对"),
        ("R04", "串项", "申报品项与订单实际构成一致", "重要", "query-meal-detail 取套餐构成"),
        ("R05", "重复报销", "同一订单不得多次申报", "重大", "按订单号建唯一性索引"),
        ("R06", "非工时订单", "下单时间落在允许时段内", "一般", "订单时间与政策时段比对"),
        ("R07", "超期申报", "在订单发生后规定天数内申报", "一般", "日期间隔 vs 申报时限"),
    ]:
        H.append(f"<tr><td><code>{rid}</code></td><td><b>{name}</b></td><td>{target}</td>"
                 f"<td><span class='sev sev-{level}'>{level}</span></td><td>{method}</td></tr>")
    H.append("</tbody></table>")

    H.append("<h2>二、缺陷清单</h2><table><thead><tr><th>编号</th><th>规则</th><th>分级</th>"
             "<th>申报单</th><th>人员/部门</th><th>应然</th><th>实然</th><th>差异</th>"
             "</tr></thead><tbody>")
    for f in data["findings"]:
        H.append(f"<tr><td><code>{esc(f['rule_id'])}</code></td><td>{esc(f['rule_name'])}</td>"
                 f"<td><span class='sev sev-{esc(f['severity'])}'>{esc(f['severity'])}</span></td>"
                 f"<td>{esc(f['claim_id'])}</td><td>{esc(f['employee'])}／{esc(f['dept'])}</td>"
                 f"<td>{esc(f['expected'])}</td><td>{esc(f['actual'])}</td><td>{esc(f['delta'])}</td></tr>")
    H.append("</tbody></table>")

    H.append("<h2>三、逐笔证据链</h2>")
    for f in data["findings"]:
        H.append(f"<div class='finding {esc(f['severity'])}'>")
        H.append(f"<h3><code>{esc(f['rule_id'])}</code> {esc(f['rule_name'])}"
                 f"<span class='sev sev-{esc(f['severity'])}'>{esc(f['severity'])}</span>"
                 f"<span class='meta'>{esc(f['claim_id'])} · {esc(f['employee'])}／{esc(f['dept'])}</span></h3>")
        H.append(f"<div class='kv'><div class='k'>应然</div><div>{esc(f['expected'])}</div>"
                 f"<div class='k'>实然</div><div>{esc(f['actual'])}</div>"
                 f"<div class='k'>差异</div><div>{esc(f['delta'])}</div></div>")
        H.append("<ul class='ev'>")
        for e in f["evidence"]:
            H.append(f"<li>{esc(e)}</li>")
        H.append("</ul>")
        H.append(f"<div class='sug'><b>整改建议：</b>{esc(f['suggestion'])}</div>")
        H.append("</div>")

    H.append("<h2>四、结论与局限</h2><div class='note-box'>")
    H.append(f"<b>结论：</b>本次共执行 7 项控制测试，覆盖 {data['claims_total']} 笔申报，"
             f"发现缺陷 {len(data['findings'])} 条，可量化敞口 {data['exposure']:.2f} 元。"
             f"其中重大缺陷 {sev_count['重大']} 条，涉及费用真实性，应优先处理并追溯系统性控制缺失。")
    H.append("<ul>")
    H.append("<li>本页为<b>脱敏样例数据</b>的运行结果，用于演示稽核逻辑，不代表任何真实审计结论。</li>")
    H.append(f"<li>政策阈值：单笔 {p['per_claim_cap']} 元 / 单人单日 {p['per_person_daily_cap']} 元 / "
             f"部门单月 {p['dept_monthly_cap']} 元 / 申报时限 {p['filing_window_days']} 天，均可配置。</li>")
    H.append("<li>本结果为<b>程序化测试</b>，不等同于审计意见；最终结论须经人工复核并签字确认。</li>")
    H.append("<li>餐品、价格及供应状态以麦当劳官方渠道实时结果为准。</li>")
    H.append("</ul></div>")

    H.append(
        "<div class='cta-foot'>"
        "<b>觉得思路有用？</b> 这个项目是开源的，Star 一下是对作者最大的支持。"
        "<div style='margin-top:10px'>"
        "<a href='https://github.com/nic-yuan/mcd-audit-officer' target='_blank' rel='noopener' "
        "style='background:var(--accent);color:#fff;text-decoration:none;border-radius:8px;"
        "padding:8px 16px;font-size:13px;font-weight:600;display:inline-block'>"
        "github.com/nic-yuan/mcd-audit-officer</a></div></div>"
    )
    H.append(f"<footer>由 mcd-audit-officer 生成 · {esc(data['generated_at'])} · "
             f"数据来源：{esc(data['data_source'])} · 参赛作品，非麦当劳官方产品</footer>")
    H.append("</div></body></html>")
    return "\n".join(H)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="demo.html")
    args = ap.parse_args()

    report = Auditor().run(
        load_sample_claims(),
        sample_facts(),
        enrich=lambda c, ctx: {"coupons": AVAILABLE_COUPONS},
        data_source="sample",
    )
    Path(args.out).write_text(build(report_to_dict(report)), encoding="utf-8")
    print(f"[ok] {args.out} 已生成（{len(report.findings)} 条缺陷）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
