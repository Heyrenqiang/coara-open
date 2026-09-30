/** 回合状态行（web 端）：Braille 旋转帧 + 轮播文案 + 已用时间。
 *
 * 位于输入框上方（与 CLI 语义一致），整行青蓝色，可点击停止当前回合。
 *
 * 布局约定：本组件无论回合是否进行中都占用同样的高度（常驻占位）。回合开始/
 * 结束时只是行内内容出现/消失，消息列表几何不变化——发送后一次滚底即到位，
 * 不会出现「spinner 行出现把最新消息顶出视口 / 触发二次滚动」的跳变。
 *
 * 帧序列只取纯 Braille 点字旋转帧（⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏，视觉上像圆点
 * 沿圆周转动）；不混入 ⦿⣿ 等几何字符，避免圆点/三角/圈圈夹杂显得杂乱。
 *
 * 仅后台任务在跑时：点击展开列表，可逐项强杀。
 */
import { useEffect, useState } from "react";
import { Button, Popconfirm, Popover, message } from "antd";
import { useStore } from "../../lib/store";
import { getWS } from "../../lib/ws";
import { killBackgroundTask } from "../../lib/api";
import { currentPhrase, refreshCustomPhrases } from "../../lib/loadingPhrases";
import {
  DURATION_HOUR_SECONDS,
  DURATION_MINUTE_SECONDS,
  DURATION_PART_SEPARATOR,
} from "../../lib/displayRules.generated";

/** 纯 Braille 旋转帧（cli-spinners dots 同源，去掉非点字几何字符）。 */
const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const FRAME_INTERVAL_MS = 80;
/** 后台任务标签轮播间隔（多任务时轮流显示名称）。 */
const BG_LABEL_ROTATE_MS = 2500;

/** 常驻行高：回合外留同样高度，避免消息列表视口随回合开始/结束跳动。 */
const STATUS_LINE_HEIGHT = 28;

type BgTaskItem = {
  task_id: string;
  kind?: string;
  label: string;
  description?: string;
  subagent_type?: string;
};

function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const two = (v: number) => String(v).padStart(2, "0");
  if (total < DURATION_MINUTE_SECONDS) return `${total}s`;
  if (total < DURATION_HOUR_SECONDS) {
    const m = Math.floor(total / DURATION_MINUTE_SECONDS);
    const s = total % DURATION_MINUTE_SECONDS;
    return `${m}m${DURATION_PART_SEPARATOR}${two(s)}s`;
  }
  const h = Math.floor(total / DURATION_HOUR_SECONDS);
  const m = Math.floor((total % DURATION_HOUR_SECONDS) / DURATION_MINUTE_SECONDS);
  const s = total % DURATION_MINUTE_SECONDS;
  return `${h}h${DURATION_PART_SEPARATOR}${two(m)}m${DURATION_PART_SEPARATOR}${two(s)}s`;
}

function backgroundPhrase(count: number, labels: string[]): string {
  const clean = labels.map((x) => String(x || "").trim()).filter(Boolean);
  if (clean.length === 0) {
    return count > 0 ? `后台 ${count} 项任务运行中` : "";
  }
  const idx = Math.floor(Date.now() / BG_LABEL_ROTATE_MS) % clean.length;
  const label = clean[idx]!;
  if (count <= 1 || clean.length <= 1) return `后台 · ${label}`;
  return `后台 ${count} 项 · ${label}`;
}

function BackgroundTaskPanel({
  items,
  killing,
  onKill,
}: {
  items: BgTaskItem[];
  killing: string | null;
  onKill: (taskId: string) => void;
}) {
  if (items.length === 0) {
    return <div style={{ padding: "4px 2px", color: "var(--coara-text-tertiary)", fontSize: 12 }}>没有运行中的后台任务</div>;
  }
  return (
    <div style={{ minWidth: 280, maxWidth: 420 }}>
      <div style={{ fontSize: 12, color: "var(--coara-text-tertiary)", marginBottom: 8 }}>运行中的后台任务</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {items.map((item) => {
          const busy = killing === item.task_id;
          return (
            <div
              key={item.task_id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                justifyContent: "space-between",
              }}
            >
              <div
                style={{
                  minWidth: 0,
                  flex: 1,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  fontSize: 13,
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                }}
                title={item.label}
              >
                {item.label}
              </div>
              <Popconfirm
                title="强行杀掉这个后台任务？"
                okText="杀掉"
                okButtonProps={{ danger: true, loading: busy }}
                showCancel={false}
                placement="topRight"
                disabled={busy}
                onConfirm={() => onKill(item.task_id)}
              >
                <Button size="small" danger loading={busy} disabled={busy}>
                  杀掉
                </Button>
              </Popconfirm>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function TurnSpinner() {
  const turnActive = useStore((s) => s.turnActive);
  const turnStartedAt = useStore((s) => s.turnStartedAt);
  const pendingCommand = useStore((s) => s.pendingCommand);
  const bgTasks = useStore((s) => Number(s.runtime?.background_tasks) || 0);
  const bgLabels = useStore((s) => {
    const raw = s.runtime?.background_task_labels;
    return Array.isArray(raw) ? raw.map((x) => String(x || "").trim()).filter(Boolean) : [];
  });
  const bgItems = useStore((s) => {
    const raw = s.runtime?.background_task_items;
    if (!Array.isArray(raw)) return [] as BgTaskItem[];
    const items: BgTaskItem[] = [];
    for (const row of raw) {
      const task_id = String(row?.task_id || "").trim();
      const label = String(row?.label || "").trim();
      if (!task_id || !label) continue;
      const item: BgTaskItem = { task_id, label };
      if (row?.kind) item.kind = String(row.kind);
      if (row?.description) item.description = String(row.description);
      if (row?.subagent_type) item.subagent_type = String(row.subagent_type);
      items.push(item);
    }
    return items;
  });
  const [, setTick] = useState(0);
  const [bgOpen, setBgOpen] = useState(false);
  const [killing, setKilling] = useState<string | null>(null);
  const active = turnActive || pendingCommand !== null;
  // 仅有后台任务在跑（无活跃回合/命令）：与正常回合复用同一旋转动画，仅文案不同
  const bgOnly = !active && bgTasks > 0;

  // 统一心跳：驱动旋转帧 + 轮播文案 + 已用时间一起刷新（帧 80ms，文案/计时随帧
  // 重读即可——文案 60s 才变，计时 1s 精度，80ms 重读成本可忽略且实现最简）。
  useEffect(() => {
    if (!active && !bgOnly) return;
    if (turnActive) {
      // 每回合开始拉一次当日 daily 定制词（后端防抖在 refreshCustomPhrases 内）。
      void refreshCustomPhrases();
    }
    const t = setInterval(() => setTick((n) => n + 1), FRAME_INTERVAL_MS);
    return () => clearInterval(t);
  }, [active, bgOnly, turnActive]);

  useEffect(() => {
    if (!bgOnly) setBgOpen(false);
  }, [bgOnly]);

  const frame = FRAMES[Math.floor(Date.now() / FRAME_INTERVAL_MS) % FRAMES.length];
  const elapsed = turnActive && turnStartedAt ? formatElapsed((Date.now() - turnStartedAt) / 1000) : "";
  const phrase = turnActive
    ? currentPhrase()
    : pendingCommand === "/compact"
      ? "正在压缩会话历史…"
      : pendingCommand !== null
        ? `正在执行 ${pendingCommand}…`
        : bgOnly
          ? backgroundPhrase(bgTasks, bgLabels)
          : "";

  const handleKill = async (taskId: string) => {
    setKilling(taskId);
    try {
      const result = await killBackgroundTask(taskId);
      if (!result.ok) {
        message.error(result.error || result.reason || "杀掉失败");
        return;
      }
      // 心跳前先本地摘掉，避免杀完还空转几秒
      const rt = useStore.getState().runtime;
      if (rt) {
        const nextItems = (rt.background_task_items || []).filter((x) => String(x?.task_id || "") !== taskId);
        const nextLabels = nextItems.map((x) => String(x.label || "").trim()).filter(Boolean);
        useStore.setState({
          runtime: {
            ...rt,
            background_task_items: nextItems,
            background_task_labels: nextLabels,
            background_tasks: nextItems.length,
          },
        });
      }
      message.success(result.label ? `已杀掉 ${result.label}` : "已杀掉");
      if ((useStore.getState().runtime?.background_tasks || 0) <= 0) setBgOpen(false);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "杀掉失败");
    } finally {
      setKilling(null);
    }
  };

  const statusInner = (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        cursor: turnActive || bgOnly ? "pointer" : "default",
        userSelect: "none",
        // 青蓝色（对齐 CLI prompt.thinking 浅色主题 var(--coara-progress)）
        color: "var(--coara-progress)",
        fontSize: 13,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        maxWidth: "100%",
        overflow: "hidden",
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ display: "inline-flex", width: 14, justifyContent: "center", alignItems: "center" }}>{frame}</span>
      <span
        style={{
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          minWidth: 0,
        }}
      >
        {phrase}
      </span>
      <span style={{ flexShrink: 0 }}>{elapsed}</span>
    </div>
  );

  return (
    // 外层与消息区同款居中容器（maxWidth 1200 + 20px padding），让 spinner
    <div
      style={{
        maxWidth: 1200,
        margin: "0 auto",
        width: "100%",
        padding: "0 20px",
        height: STATUS_LINE_HEIGHT,
        display: "flex",
        alignItems: "center",
      }}
    >
      {active || bgOnly ? (
        turnActive ? (
          <Popconfirm
            title="停止当前回合？"
            okText="停止"
            okButtonProps={{ danger: true }}
            showCancel={false}
            placement="topLeft"
            onConfirm={() =>
              getWS().send({
                type: "interrupt",
                workspace_dir: useStore.getState().workspaceDir ?? undefined,
              })
            }
          >
            {statusInner}
          </Popconfirm>
        ) : bgOnly ? (
          <Popover
            trigger="click"
            placement="topLeft"
            open={bgOpen}
            onOpenChange={setBgOpen}
            content={<BackgroundTaskPanel items={bgItems} killing={killing} onKill={(id) => void handleKill(id)} />}
          >
            {statusInner}
          </Popover>
        ) : (
          statusInner
        )
      ) : null}
    </div>
  );
}
