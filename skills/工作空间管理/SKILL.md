---
listed: false
name: 工作空间管理
description: >-
  coara Home 侧配置：events/webhook/file_watch YAML、移出前禁用事件、AGENTS 模板、体检 checklist。
  建删列工作空间、看动态、工作空间排班用 ws；个人口头提醒用 reminder；改代码 delegate(coaras, workspace=alias)。
---

# 工作空间管理

本技能 **不重复** `ws` 工具已覆盖的登记/移出操作。  
**对话内建删列工作空间** → 先 `tool(action="activate", name="ws")`（挂起工具），再按 `ws` 工具描述调用。  
**本技能** 只管 `ws` **够不着** 的配置（事件源定义 `<workspace>/.coara/matters/definitions/`、webhook、移出检查清单）。

## alias 贯穿全链路

| 环节 | 落点 |
|------|------|
| 在册登记 | `{coara_home}/registry/workspaces.yaml` |
| 事件绑定 | `<workspace>/.coara/matters/definitions/*.yaml` 的 `workspace:`（空间自治；旧集中布局启动时迁移） |
| 工作空间动态 | `<workspace>/.coara/inbox/` |
| 跨空间工程委派 | `delegate(..., workspace="{alias}")` |

### 当前 CLI 激活 vs 跨工作空间

- 在哪开 `coara`，哪条 alias 标为「当前 CLI 激活」；trace/log 跟启动 cwd 走。
- 处理另一工作空间：`review(action=list)`（janitor 专属）、`delegate(coaras, workspace=其它alias)` — cwd 不变。
- 用户只问「在哪个工作空间」→ `ws(list)` 后回答；**不要** `ws(switch)`。只有用户明确说切换/进入时才 `switch`。

---

## 何时 activate 本技能

- ✓ 配置 / 调整 **webhook、file_watch**（`<workspace>/.coara/matters/definitions/*.yaml`）
- ✓ **移出**带事件源的工作空间（先禁用 events，再 `ws(remove)`）
- ✓ **体检** webhook、`workspace:` 与 alias 一致
- ✓ 新工作空间需要 **AGENTS.md 模板**

## 不必 activate

- 列/建/移工作空间 → **`ws` 足够**（`ws` 本身是挂起工具，用前先 `tool(activate, name="ws")`）
- 只改代码 → `ws(list)` + `delegate(coaras, workspace=alias)`

---

## Webhook 与 salience / handle

`read`/`write` 通常够不着 `<workspace>/.coara/matters/definitions`，用 **shell** 编辑。

事件内容**无条件**写入 `<workspace>/.coara/inbox/`（`review(action=list)` 查看），再由两个字段决定曝光与处置

| 字段 | 取值 | 行为 |
|------|------|------|
| `handle` | **`park`**（推荐/默认） | 挂住等用户，不打扰 Root |
| `handle` | `janitor` | 叫醒管家 janitor 过目（勾掉/呈阅/自处理，写处置轨迹） |
| `ttl_seconds` | — | 消息保质期：过期未读自动 dismissed（留轨迹） |
| `salience` | `low` / `normal`（默认）/ `high` | `high` 上浮跨空间前台待处理视图（`review(action=pending)`） |

旧 `trigger_mode` 已废弃并被忽略（处置缺省 `handle: park`）；不再有「事件自动叫醒主会话跑回合」。

事件 YAML 的 `workspace` **必须等于** registry **alias**（定义落在该空间 `.coara/matters/definitions/`）。

---

## 移出 checklist（有事件源）

1. `ws(list)` — 确认 alias 与事件 id
2. **主动询问用户**：是否同时删除磁盘目录（默认只取消登记）
3. shell 禁用 events yaml → `/events reload`
4. `ws(action="remove", name=...)`；用户确认删盘时加 `delete_disk=true`
5. `ws(list)` 验证

---

## AGENTS.md 最小模板

登记用 `ws(add)`；无 AGENTS 时用 `write` 或下方模板补全。

```markdown
# 工作空间名

## 概述
一行说明。

## 维护约定
- `delegate(coaras, workspace="<alias>")`

## 构建与验证
- 构建：`[待填]`
- 测试：`[待填]`
```

---

## 禁止事项

- 不要覆盖未同意的 webhook secret
- 不要用 `write` 改 registry（用 `ws add/remove`）
- 移出不删磁盘目录
- 看动态用 `review(action=list)`（janitor）或 `/ws updates`（用户），不要 grep 工作空间目录
