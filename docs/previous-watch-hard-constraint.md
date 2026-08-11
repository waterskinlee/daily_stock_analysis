# 上次分析观察点硬约束升级方案

> 状态：设计稿，待实施。对应 `src/analysis_previous_context.py` docstring 与 CHANGELOG 里"后续可评估升级为结构化核对字段并纳入报告完整性校验"。

## 1. 背景与问题

当前实现（commit `0a92fd4`）把上次分析的观察条件、狙击点位注入本次 prompt，要求 LLM 逐条核对「已兑现 / 未兑现 / 部分兑现」。实测（task `4c6ae45b…5cf7`，工业富联，id 176）证明：

- **注入成功**：`context_snapshot.enhanced_context.previous_analysis_context` 包含完整 section。
- **模型未执行**：`raw_result` 全文 0 处「已兑现 / 未兑现 / 部分兑现 / 上次」。
- **原因**：这是"软约束"——提示词要求但不强制，完整性校验（`check_content_integrity`）不检查核对结果，模型可以选择性忽略。

## 2. 目标

把"核对上次观察点"从提示词软约束升级为**结构化硬约束**：LLM 必须输出结构化的 `previous_watch_verification` 字段，完整性校验缺失或不合规时触发重试/占位补全，与现有 `phase_decision` 七字段一致。

## 3. 非目标

- 不引入新的 DB 列（核对结果只存 `raw_result.dashboard.previous_watch_verification`，随报告一起持久化）。
- 不做事后核对正确性校验（不验证"已兑现"判断是否正确，只校验字段存在与结构）。
- 不强制 Agent 路径做 LLM 重试（Agent 路径保持 `agent_weak` 占位补全语义，见 §7）。
- 不改变软约束的 fail-open 行为：回读失败、无上次记录、记录过期时，不阻塞流程。

## 4. 数据模型

### 4.1 新增结构化字段：`dashboard.previous_watch_verification`

```json
"previous_watch_verification": {
    "has_previous": true,
    "previous_analysis_time": "2026-08-10 18:06",
    "items": [
        {
            "condition": "跌破 1450 止损离场",
            "status": "fulfilled|not_fulfilled|partially_fulfilled|stale",
            "evidence": "今日最低价 1452，未跌破，止损未触发",
            "impact": "维持原止损位，本次未离场"
        }
    ],
    "summary": "上次观察点整体未触发，本次维持原判"
}
```

字段语义：
- `has_previous: bool` — 本次分析是否存在可回读的上次观察点。**必须与 prompt 是否注入 `previous_analysis_context` 一致**。
- `previous_analysis_time: str | null` — 上次分析时间（从注入 section 复述，便于核对）。
- `items: list[VerificationItem]` — 逐条核对结果。
  - `condition: str` — 上次观察条件原文（截断 120 字）。
  - `status: enum` — `fulfilled` / `not_fulfilled` / `partially_fulfilled` / `stale`（超过 10 天过期）。
  - `evidence: str` — 今日行情/数据中的核对依据（≤200 字）。
  - `impact: str` — 对本次决策的影响（≤120 字）。
- `summary: str` — 整体核对结论（≤120 字）。

### 4.2 空状态契约

| 场景 | `has_previous` | `items` | 校验行为 |
|---|---|---|---|
| 有上次记录，注入了 section | `true` | 非空，每条含 4 字段 | 必须完整 |
| 无上次记录（首次分析/回读失败/过期） | `false` | `[]` | 仅校验 `has_previous=false` |
| 上次记录全部过期（>10 天） | `true` | `[{status: "stale"}]` 或 `[]` + `summary` 说明 | 允许 `items=[]` 但 `summary` 必须说明 |

关键不变式：**`has_previous` 必须与 pipeline 是否注入 `previous_analysis_context` 一致**。pipeline 在注入 section 时同步在 context 里标记 `previous_watch_injected=True`，完整性校验读取该标记决定是否要求 `has_previous=true`。

## 5. 配置

新增开关 `ANALYSIS_PREVIOUS_WATCH_HARD`（默认 `false`，向后兼容）：

- `false`（默认）：软约束，保持现状行为（注入 section，但不校验核对结果字段）。
- `true`：硬约束，`check_content_integrity` 校验 `previous_watch_verification` 字段，缺失/不合规走重试/占位补全。

`ANALYSIS_PREVIOUS_WATCH_ENABLED=false` 时，整个特性关闭，不注入 section 也不校验。硬开关依赖软开关：`hard=True` 但 `enabled=False` 时，硬约束不生效（无 section 可核对）。

## 6. 完整性校验扩展

### 6.1 `check_content_integrity` 新增参数

```python
def check_content_integrity(
    result: "AnalysisResult",
    *,
    require_phase_decision: bool = False,
    require_previous_watch_verification: bool = False,  # 新增
) -> Tuple[bool, List[str]]:
```

新增校验规则（仅当 `require_previous_watch_verification=True`）：
- `dash.get("previous_watch_verification")` 必须是 dict。
- `has_previous` 必须是 bool。
- 若 `has_previous=true`：`items` 必须是 list 且非空；每个 item 的 `condition`/`status`/`evidence`/`impact` 非空字符串；`status` ∈ 4 个合法枚举。
- 若 `has_previous=false`：`items` 必须是空 list。
- `summary` 非空字符串。

缺失字段加入 `missing_fields`，走现有重试/占位补全流程。

### 6.2 触发条件

pipeline 调用 `check_content_integrity` 时传 `require_previous_watch_verification`：

```python
require_previous_watch = (
    getattr(config, "analysis_previous_watch_hard", False)
    and getattr(config, "analysis_previous_watch_enabled", True)
    and bool(context.get("previous_watch_injected"))
)
```

只有「硬开关开 + 软开关开 + 实际注入了 section」三重条件才校验，避免无上次记录时误报。

## 7. 两条路径的接线

### 7.1 Legacy LLM 路径（`analyzer.py`）

- `_format_prompt`：在 `previous_analysis_context` section 后追加结构化输出要求（见 §8）。
- 完整性校验重试循环（`analyzer.py:3558-3644`）：`require_phase_decision` 旁加 `require_previous_watch_verification`，走现有重试 + 占位补全。
- `apply_placeholder_fill`：新增 `dashboard.previous_watch_verification.*` 占位分支。

### 7.2 Agent 路径（`pipeline.py`）

- 注入 section 时同步标记 `initial_context["previous_watch_injected"] = True`。
- Agent 弱完整性（`pipeline.py:1628-1640`）：`require_previous_watch_verification` 传入，缺失走 `apply_placeholder_fill`（无 LLM 重试，与现有 `agent_weak` 语义一致）。
- `executor._build_user_message`：在 section 后追加结构化输出要求。

## 8. 提示词模板扩展

### 8.1 `format_previous_analysis_section` verify 指令追加

在现有 `> 请逐条核对…` 段落追加：

```
> 输出要求：必须在 `dashboard.previous_watch_verification` 字段输出结构化核对结果：
> - has_previous: true（本次已注入上次观察点）
> - items: 数组，每条含 condition（上次条件原文）、status（fulfilled/not_fulfilled/partially_fulfilled/stale）、evidence（今日数据依据）、impact（对本次决策影响）
> - summary: 整体核对结论
> 若无上次观察点，输出 has_previous=false, items=[], summary 说明原因。
```

### 8.2 Dashboard JSON 模板新增字段

三处模板（`analyzer.py` 两个变体 + `executor.py` 两个变体）的 `dashboard` 对象内追加：

```json
"previous_watch_verification": {
    "has_previous": true,
    "previous_analysis_time": "YYYY-MM-DD HH:MM",
    "items": [
        {
            "condition": "上次观察条件原文",
            "status": "fulfilled/not_fulfilled/partially_fulfilled/stale",
            "evidence": "今日行情/数据中的核对依据",
            "impact": "对本次决策的影响"
        }
    ],
    "summary": "整体核对结论"
}
```

## 9. 报告渲染

`notification.py` 在 `phase_decision` 块后新增 `_append_previous_watch_verification_block`，渲染核对结果表格（条件 / 状态 / 依据 / 影响），状态用 emoji（✅ 已兑现 / ❌ 未兑现 / ⚠️ 部分兑现 / ⏳ 过期）。Jinja 模板同步新增对应块。

## 10. 影响面与回归

| 文件 | 改动 |
|---|---|
| `src/schemas/report_schema.py` | 新增 `PreviousWatchVerification` / `VerificationItem` 类，`Dashboard` 增字段 |
| `src/config.py` | 新增 `analysis_previous_watch_hard: bool = False` + env 解析 |
| `src/core/config_registry.py` | 新增 `ANALYSIS_PREVIOUS_WATCH_HARD` 条目 |
| `src/analysis_previous_context.py` | `format_previous_analysis_section` 追加结构化输出指令 |
| `src/analyzer.py` | `check_content_integrity` + `apply_placeholder_fill` + `_build_integrity_complement_prompt` + 两个 dashboard 模板 |
| `src/core/pipeline.py` | 两条路径注入 `previous_watch_injected` 标记 + 传 `require_previous_watch_verification` |
| `src/agent/executor.py` | `_build_user_message` 追加结构化输出要求 + dashboard 模板增字段 |
| `src/notification.py` | 新增 `_append_previous_watch_verification_block` |
| `templates/report_markdown.j2` / `report_wechat.j2` | 新增渲染块 |
| `.env.example` | 新增 `ANALYSIS_PREVIOUS_WATCH_HARD` |
| `apps/dsa-web/src/locales/settingsHelp.ts` | 新增/更新条目（zh + en） |
| `docs/CHANGELOG.md` | 记录升级 |
| `tests/test_analysis_previous_context.py` | 扩展注入 + 校验测试 |
| `tests/`（新增或现有 integrity 测试） | 硬约束校验、占位补全、空状态契约测试 |

## 11. 测试计划

- **空状态**：无上次记录 → `has_previous=false, items=[]`，校验通过。
- **注入但模型未输出字段**：校验失败 → missing_fields 含 `dashboard.previous_watch_verification` → 重试/占位。
- **注入且模型输出完整**：校验通过。
- **`items` 非空但缺 `status`/`evidence`**：校验失败。
- **`status` 非法枚举**：校验失败。
- **`hard=False`**：不校验，保持软约束行为。
- **`enabled=False`**：不注入 section，不校验。
- **占位补全**：`has_previous=true` 时占位为 `{"has_previous": true, "items": [], "summary": "模型未提供核对结果"}`。
