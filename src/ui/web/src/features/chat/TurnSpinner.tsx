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
 */
import { useEffect, useState } from "react";
import { Popconfirm } from "antd";
import { useStore } from "../../lib/store";
import { getWS } from "../../lib/ws";
import { currentPhrase, refreshCustomPhrases } from "../../lib/loadingPhrases";

/** 纯 Braille 旋转帧（cli-spinners dots 同源，去掉非点字几何字符）。 */
const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const FRAME_INTERVAL_MS = 80;

/** 常驻行高：回合外留同样高度，避免消息列表视口随回合开始/结束跳动。 */
const STATUS_LINE_HEIGHT = 28;

function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}m ${String(s).padStart(2, "0")}s`;
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
}

export function TurnSpinner() {
  const turnActive = useStore((s) => s.turnActive);
  const turnStartedAt = useStore((s) => s.turnStartedAt);
  const pendingCommand = useStore((s) => s.pendingCommand);
  const [, setTick] = useState(0);
  const active = turnActive || pendingCommand !== null;

  // 统一心跳：驱动旋转帧 + 轮播文案 + 已用时间一起刷新（帧 80ms，文案/计时随帧
  // 重读即可——文案 60s 才变，计时 1s 精度，80ms 重读成本可忽略且实现最简）。
  useEffect(() => {
    if (!active) return;
    if (turnActive) {
      // 每回合开始拉一次当日 daily 定制词（后端防抖在 refreshCustomPhrases 内）。
      void refreshCustomPhrases();
    }
    const t = setInterval(() => setTick((n) => n + 1), FRAME_INTERVAL_MS);
    return () => clearInterval(t);
  }, [active, turnActive]);

  const frame = FRAMES[Math.floor(Date.now() / FRAME_INTERVAL_MS) % FRAMES.length];
  const elapsed = turnActive && turnStartedAt ? formatElapsed((Date.now() - turnStartedAt) / 1000) : "";
  const phrase = turnActive ? currentPhrase() : pendingCommand === "/compact" ? "正在压缩会话历史…" : `正在执行 ${pendingCommand}…`;

  return (
    // 外层与消息区同款居中容器（maxWidth 1200 + 20px padding），让 spinner
    // 与聊天气泡左缘对齐。高度恒为 STATUS_LINE_HEIGHT（回合外也占位）。
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
      {active ? (
        (() => {
          const inner = (
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                cursor: turnActive ? "pointer" : "default",
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
              <span style={{ display: "inline-block", width: 14 }}>{frame}</span>
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
          // 命令执行（/compact 等）不是回合，interrupt 停不掉——不包停止确认
          if (!turnActive) return inner;
          return (
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
              {inner}
            </Popconfirm>
          );
        })()
      ) : null}
    </div>
  );
}
