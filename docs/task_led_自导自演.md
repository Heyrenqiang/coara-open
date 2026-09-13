# task-led 任务驱动自导自演（makevideo 高级形态）

> **定位**：自演示方向总纲见 [`具身工具架构.md`](./具身工具架构.md)（coara 有手有嘴，外部手机旁观捕捉）。本档收敛到「coara 自己开启录制、自己真实干活、自己讲解、自己收尾成片」这一具体可落地形态的内核、外部边界与对轨逻辑。
> **状态**：**已落地（具身架构版）**。总纲见 `docs/具身工具架构.md`——后期配音/字幕/合成思路已废弃，voice/subtitle 是调用即真实发生的具身器官，同步天然。本档保留 task-led 流程框架，各节已对齐具身架构。
> **日期**：2026-08-22（具身对齐同日）。

## 1. 一句话定义

> 用户发一条指令 → coara 自己 `record start` 开录 → 自己真实执行任务（写代码/网页/PPT 等）→ 每一步用 **voice 器官放声讲解、subtitle 器官同步烧字** → 任务结束自己 `record stop`（voice 留底自动混流）→ 交付原片。

**素材 = coara 真实执行任务时的操作系统画面**，不是预写死的键鼠模拟。这是与「脚本驱动」的本质区别。

## 2. 与「脚本驱动」的对照

| 维度 | 脚本驱动（旧/已弃用形态） | task-led（本档） |
|------|--------------------------|-----------------|
| 素材来源 | 预写死的键鼠动作序列 | coara 真实执行任务的工具轨迹 |
| 录制内容 | 排练过的动作（内容有限） | 任意任务（写代码/网页/PPT，无限扩展） |
| 触发 | 外部脚本一次性跑完 | coara 自己开录/停录 |
| 讲解 | 预设旁白脚本 | voice 器官边干边讲（实时放声） |
| 字幕 | 后期按时间轴烧 SRT | subtitle 器官实时烧字上屏 |
| 同步 | 分镜 mark + 后期对轨（易错） | **天然同步**——声音/字幕/操作同刻真实发生 |
| 与 coara 关系 | 脱钩（外部进程） | coara 是主体（自导自演） |

## 3. 核心洞察：为什么不需要「额外操作自己」

coara 干活靠工具（`read`/`write`/`edit`/`shell`/`web`…），这些工具的执行逻辑会**实时刷在 coara 所在终端窗口**。只要录制一直开着，coara 干活的过程就天然入镜。

「操作自己」的成立条件是：**coara 是自己任务的管理者，录制是其任务的一部分**——开录、干活、讲解、停录都是 coara 这个 agent 自己在推进，不是外部脚本替它做。用户角色只剩一句指令。

## 4. 分层架构

- **用户**：一句指令「录一个…视频」。
- **self_demo 技能**（`skills/self_demo/SKILL.md`）：演示意识与器官使用规范——这是 coara 需要的唯一增量。
- **makevideo 引擎**（`standalone/makevideo/`，独立 CLI）：record（被动捕捉）/ actuate（手）/ voice（嘴巴）/ subtitle（字）/ mark（章节）。
- **操作系统**：屏幕/键鼠/扬声器/FFmpeg/文件。

**coara 是主体**：整个流程由 coara 用器官推进；引擎不依赖 coara 内部状态。

## 5. 核心链路（coara 视角）

1. **收指令**：用户说「录一个 X 视频」（X=写代码/做网页/做 PPT…）。
2. **开录**：`record start`。
3. **干任务**：coara 正常执行 X 的真实任务（用常规工具），过程实时上屏。
4. **讲+写**：每个关键步骤——`embody` 的 voice 放声讲「这一步在做什么」，同文字幕协同上屏（caption 默认开）。边说边做时 voice 传 `wait=false` 后台讲。
5. **记轨迹**：actions.jsonl 自动记录每次器官调用的时刻与内容（无需人工对轨）。
6. **停录**：`record stop`——voice 留底（voice_<t>.mp3）自动按时间戳混流进原片音轨。
7. **交付**：video.mp4 原片即成片，无后期环节。

## 6. 讲解与字幕：实时器官（方案 A 已落地）

旧档的方案 A/B 之争在具身架构下不成立——**实时即答案**：

- voice 调用即放声（edge_tts 流式 + ffplay；离线回退 SAPI，同样留底），与画面动作同刻发生
- 字幕不是独立器官：voice 的 `caption` 参数（默认开）同文烧字、说完即隐，声字天然对齐
- 讲解文本既被听见也被看见，且与产生它的动作在同一时刻进入 actions.jsonl

## 7. 对轨逻辑（已简化为「无需对轨」）

- **旧（脚本驱动）**：分镜脚本 + `mark` 镜头边界 + 预设旁白 + 后期对轨。
- **新（具身）**：声音/字幕/操作同刻真实发生，同步天然。actions.jsonl 的轨迹时间戳仅用于回看定位与 voice 留底混流；`mark` 保留为章节标记。

## 8. 与现有代码的映射

| 层 | 现状 | 说明 |
|----|------|------|
| record | `engine/session.py` + `recorder_host.py` | 被动捕捉（gdigrab 录屏），停录自动混流 voice 留底；**coara 侧经 `screenshot` 工具调用（record_start/stop/status，截屏套：单帧 vs 连续捕捉）** |
| actuate（手） | `engine/actuate.py` | 实时键鼠，已就绪 |
| voice（嘴巴） | `engine/speak.py` + `narrate.py` | 实时放声：edge 流式 + ffplay，SAPI 兜底（先合成 wav 再播，留底对齐）；narrate 协同字幕（长显示+说完即隐）；coara 侧经挂起工具 `embody` 直调 |
| subtitle（字） | `engine/caption.py` + `caption_host.py` | 字幕浮层（点击穿透、闲置自退）；非独立器官，由 voice 的 caption 参数驱动 |
| CLI | `__main__.py` | actuate/voice/subtitle/mark 四命令（record 已进 coara `screenshot` 截屏套工具；CLI 保留作独立备用与 embody `wait=false` 的 worker） |
| 技能 | `skills/self_demo/SKILL.md` | 演示意识 + 器官使用规范（已对齐具身架构） |
| 后期模块 | **已拆除** | 文件配音/SRT/compose/timeline/scenes 全部删除 |

## 9. 可录性结论（本机实测）

- 本机 ffmpeg 9.0（Gyan）**无 wasapi**，声卡**无立体声混音** → 系统声音无法被录屏实时回采
- 解法：voice 放声同时 Tee 留底 `voice_<t>.mp3`（t=放声时刻，相对 record_t0），停录自动 `adelay+amix` 混流，视频流直通不重编码——同步天然
- 若改用真手机拍摄（麦克风收环境声）：无需留底混流，拍到的就是原片
