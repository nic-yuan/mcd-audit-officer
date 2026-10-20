# 麦审官 · mcd-audit-officer

> 基于麦当劳 MCP 的**企业团餐与订餐报销稽核 Skill**
> 把内控审计的穿行测试方法，落到"订餐报销"这个具体场景。

**不是又一个省钱工具。** 市面上绝大多数麦当劳 MCP 项目在做"怎么点更便宜"，这个做的是另一件事：**你报上来的这笔钱，是不是真的、准的、合规的。**

> **[▶ 在线看一份真实的稽核底稿](https://nic-yuan.github.io/mcd-audit-officer/)** — 免安装、免登录，打开就是一次完整稽核的结果：22 条缺陷、三级分级、每条带证据链。

---

## 它解决什么

订餐报销有三个天然漏洞，而且都很难靠人工发现：

1. **价格无法独立验证**。审核人只能看票面金额，不知道这顿饭官方核价到底多少。报销人说多少就是多少。
2. **一单多报难发现**。同一订单号在几十笔申报里出现两次，肉眼扫不出来。
3. **品项可以虚增**。申报单上写了 5 样，订单里只有 4 样，没人逐项比对。

本 Skill 用**三单交叉**解决：

```
   申报单（应然 / 报销人自述）
 × 订单事实（实然 / 系统记录）
 × 官方核价（基准 / 麦当劳 MCP calculate-price）
 ─────────────────────────────
 = 带证据链与缺陷分级的稽核底稿
```

第三条是关键——**麦当劳官方 `calculate-price` 提供了一个独立于报销人的权威比价基准**，这是纯人工审核拿不到的。

---

## 30 秒跑起来（不需要任何配置）

```bash
git clone <your-repo-url>
cd mcd-audit-officer
python scripts/run_audit.py
```

**零第三方依赖，纯 Python 标准库。** 没有 MCP Token 也能完整跑通——内置脱敏样例数据，直接输出一整份底稿。

```bash
# 用你自己的申报表
python scripts/run_audit.py --csv examples/expense_sample.csv

# 接真实 MCP 数据
export MCD_MCP_TOKEN=你的Token
python scripts/run_audit.py --csv my_claims.csv --source mcp

# 自检 MCP 连通性
python scripts/run_audit.py --probe

```bash
# 输出 JSON 进台账
python scripts/run_audit.py --json result.json

# 重新生成可视化 demo（双击 demo.html 即可打开）
python scripts/gen_demo.py
```

> **可视化 demo**：`demo.html` 是零依赖单页报告，把一次稽核结果渲染成可直接阅读的底稿。
> 不引 CDN、不跑 JS 框架，双击就能看。想直接看效果而不想装环境，开这个就够。

---

## 输出长什么样

`examples/expected_output.md` 是完整样例，节选：

```markdown
# 订餐报销稽核底稿

- 申报笔数：12　申报总额：701.00 元
- 缺陷合计：**22** 条 （重大 2 / 重要 9 / 一般 11）
- 可量化敞口：**117.00 元**

## 二、缺陷清单

| 编号 | 规则 | 分级 | 申报单 | 人员/部门 | 应然 | 实然 | 差异 |
|---|---|---|---|---|---|---|---|
| R05 | 重复报销 | **重大** | C-001 | 员工A／研发部 | 同一订单仅可申报一次 | 该订单号共申报 2 次 | 1 |
| R01 | 价差异常 | **重要** | C-003 | 员工C／市场部 | 官方核价 112.00 元 | 申报 128.00 元 | 16.00 |
| R03 | 超预算 | **重要** | C-006 | 员工E／运营部 | 单笔上限 80.00 元 | 单笔实际 95.00 元 | 15.00 |
```

每一条缺陷都带完整证据链：

```markdown
### R05 · 重复报销（重大）· C-001

- 应然：同一订单仅可申报一次
- 实然：该订单号共申报 2 次
- 证据：订单号 MCD-SAMPLE-0001
- 证据：重复申报单号：C-004
- 证据：涉及金额合计 85.00 元
- **整改建议**：立即冻结相关付款，逐笔核实后追回；同步检查报销系统是否缺少
  订单号唯一性校验（系统性控制缺陷）。
```

---

## 七个控制点

| 编号 | 控制点 | 控制目标 | 分级 | 取证方式 |
|---|---|---|---|---|
| R01 | 价差异常 | 申报金额与官方核价一致 | 重要 | `calculate-price` 重算应付总额 |
| R02 | 券未核销 | 可用券应充分使用 | 一般 | `query-my-coupons` 比对已核销券 |
| R03 | 超预算 | 单笔/单人单日/部门单月不超标准 | 重要 | 三级阈值汇总比对 |
| R04 | 串项 | 申报品项与订单实际构成一致 | 重要 | `query-meal-detail` 取套餐构成 |
| R05 | 重复报销 | 同一订单不得多次申报 | **重大** | 按订单号建唯一性索引 |
| R06 | 非工时订单 | 下单时间落在允许时段内 | 一般 | 订单时间与政策时段比对 |
| R07 | 超期申报 | 在订单发生后规定天数内申报 | 一般 | 日期间隔 vs 申报时限 |

缺陷三级：**重大**（真实性，可能导致资产损失）/ **重要**（准确性）/ **一般**（合规与时效）。

---

## 接入真实数据

1. 申请 Token：https://open.mcd.cn/mcp （手机号登录 → 控制台 → 激活，免费）
2. `export MCD_MCP_TOKEN=你的Token`
3. `python scripts/run_audit.py --source mcp`

申报表 CSV 列格式：

```
claim_id,employee,dept,order_no,order_date,claim_date,declared_amount,declared_items,purpose,meal_type
```

`declared_items` 用 `|` 分隔。样例见 `examples/expense_sample.csv`。

政策阈值全部可配（`--policy policy.json`）：

```json
{
  "per_claim_cap": 80.0,
  "per_person_daily_cap": 150.0,
  "dept_monthly_cap": 8000.0,
  "price_tolerance_abs": 1.0,
  "price_tolerance_pct": 0.02,
  "allowed_start": "07:00",
  "allowed_end": "21:00",
  "filing_window_days": 30,
  "coupon_check_min_amount": 40.0
}
```

---

## 目录结构

```
mcd-audit-officer/
├── SKILL.md                    # Agent Skill 定义（可直接装入 WorkBuddy 等支持 Skill 的客户端）
├── README.md
├── CONTEST_DECLARATION.md      # 参赛声明（官方原文，未改动）
├── MCP_INTEGRATION.md          # MCP Tool 使用与业务价值
├── mcp-config.example.json     # 脱敏配置，仅环境变量占位符
├── workbuddy.md                # WorkBuddy 开发对话上下文
├── scripts/
│   ├── audit_engine.py         # 稽核引擎：7 条规则 + 分级 + 底稿渲染
│   ├── mcp_client.py           # MCP 客户端（标准库实现，Streamable HTTP）
│   ├── sample_data.py          # 脱敏样例数据
│   └── run_audit.py            # CLI 入口
├── references/
│   ├── control-points.md       # 控制点台账与测试方法
│   ├── defect-grading.md       # 缺陷分级标准
│   └── output-template.md      # 底稿模板
└── examples/
    ├── expense_sample.csv      # 样例申报表
    └── expected_output.md      # 对应输出底稿
```

---

## 设计原则

1. **零依赖**。纯 Python 标准库，clone 下来就能跑，不卡环境。
2. **可离线复现**。审计结论必须能重放，这是内控的基本要求。
3. **没有证据不下结论**。每条缺陷强制带 `evidence` 字段。
4. **降级必须可见**。MCP 取数失败时底稿会显式标注 `数据来源：sample`，绝不伪装成实时数据。
5. **无法取证 ≠ 无缺陷**。取不到订单的申报单列进"局限"，不给出虚假的干净结论。

---

## 局限

- 本工具输出的是**程序化测试结果，不等同于审计意见**，最终结论须经人工复核签字。
- 样例数据全部虚构，不含任何真实订单号、真实人名或真实门店。
- 餐品、价格、供应状态以麦当劳官方渠道实时结果为准。
- 非麦当劳官方产品，项目输出仅供参考。

---

## 许可

MIT License。参赛作品，由参赛者独立开发。
