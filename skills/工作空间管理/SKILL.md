---
name: 工作空间管理
description: >-
  coara Home 侧配置：事件源 YAML、移出前禁用事件、AGENTS 模板、体检 checklist。
  建删列工作空间、看动态用 ws；个人提醒用 reminder；改代码用 delegate(coaras, workspace=alias)
---

# 工作空间管理

本技能不重复 `ws` 已覆盖的登记/移出。  
对话内建删列工作空间 → 先 `tool(action="activate", name="ws")`，再按 `ws` 描述调用。  
本技能管 `ws` 够不着的配置：事件源定义、webhook、移出检查清单、AGENTS 模板。

## alias 贯穿全链路

| 环节 | 落点 |
|------|------|
| 在册登记 | `{coara_home}/registry/workspaces.yaml` |
| 事件绑定 | `<workspace>/.coara/matters/definitions/*.yaml` 的 `workspace:` |
| 工作空间动态 | `<workspace>/.coara/inbox/` |
| 跨空间工程委派 | `delegate(..., workspace="{alias}")` |

### 当前激活 vs 跨空间

- 各端有自己的当前工作空间视图；trace / 日志跟该空间数据目录走
- 处理另一空间：`/ws updates list <其它 alias>`、`delegate(coaras, workspace=其它 alias)` — 不必先 switch
- 用户只问「在哪个工作空间」→ `ws(list)` 后回答；不要擅自 `ws(switch)`。只有用户明确说切换/进入时才 `switch`

---

## 何时 activate

- 配置 / 调整 webhook、file_watch、cron 等事件源定义
- 移出带事件源的工作空间（先禁用 events，再 `ws(remove)`）
- 体检：webhook、`workspace:` 与 alias 一致
- 新工作空间需要 AGENTS.md 模板

## 不必 activate

- 列/建/移工作空间 → `ws` 足够（挂起工具，用前 `tool(activate, name="ws")`）
- 只改代码 → `ws(list)` + `delegate(coaras, workspace=alias)`
- 细抠事件源字段 → 优先 `event-source` 技能 + SPEC

---

## 事件源 salience / handle

事件定义落在 `<workspace>/.coara/matters/definitions/`。`read`/`write` 若够不着，用 shell。

事件内容写入 `<workspace>/.coara/inbox/`（`/ws updates list` 查看），由两个字段决定曝光与处置：

| 字段 | 取值 | 行为 |
|------|------|------|
| `handle` | `park`（默认） | 挂住等用户 |
| `handle` | `janitor` | 预留；落箱后不派 LLM，仍等 `/ws updates` 处置 |
| `ttl_seconds` | — | 过期未读自动 dismissed（留轨迹） |
| `salience` | `low` / `normal`（默认）/ `high` | `high` 进前台待处理视图（`/ws updates pending`） |

YAML 的 `workspace` 必须等于 registry **alias**。

---

## 移出 checklist（有事件源）

1. `ws(list)` — 确认 alias 与事件 id
2. 询问用户：是否同时删除磁盘目录（默认只取消登记）
3. 禁用 events yaml → `/events reload`
4. `ws(action="remove", name=...)`；确认删盘时加 `delete_disk=true`
5. `ws(list)` 验证

---

## AGENTS.md 最小模板

登记用 `ws(add)`；无 AGENTS 时用 `write` 补全。

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

## 禁止

- 不要覆盖未同意的 webhook secret
- 不要用 `write` 改 registry（用 `ws add/remove`）
- 移出默认不删磁盘目录
- 看动态用 `/ws updates list`，不要 grep 工作空间目录当收件箱
