/** 聊天区「该不该滚底」的判据与滚动锚的记法（纯函数）。
 *
 * 为什么单独一个模块：这段判断原本散在组件里（锚点还原 effect、节流跟随 effect、
 * 发送回底三处各判一次），结果「刷新该贴底」被上一页面生命留在 sessionStorage 里的
 * 滚动锚顶掉——骨架换成真实内容后列表高度变了，旧位置一还原就落到页面最上方。
 *
 * 抽成纯函数后，四种场景（刷新 / 存活期上翻 / 空间切换 / 骨架转真实内容）都能被
 * selftest 直接断言；组件只负责按结论做时序（layout effect + rAF）。
 *
 * 滚动锚（ScrollAnchor）不是绝对像素而是「贴底 / 某条消息 + 相对偏移」：
 * 内容高度一变（他端补历史、图片与公式撑高、折叠区展开、渲染窗口回收），同一个
 * scrollTop 就落到别的逻辑位置上；锚在消息上才与「用户在看哪一条」等价。
 */

type ScrollDecision = "bottom" | "anchor" | "hold";

/** 滚动锚：切走 / 上翻时「停在哪儿」的声明式记法。 */
export interface ScrollAnchor {
  /** bottom＝贴底锚（还原时继续贴底）；message＝内容锚（钉在某条消息上） */
  kind: "bottom" | "message";
  /** 内容锚钉住的那条消息的稳定 key（服务端键）；贴底锚为空串 */
  key: string;
  /** 内容锚：该消息顶边相对滚动容器视口顶部的偏移（px；负数＝已滚过去一截） */
  offset: number;
}

/** 贴底锚（列表为空 / 贴着底 / 量不到几何时的答案）。 */
export const BOTTOM_ANCHOR: ScrollAnchor = { kind: "bottom", key: "", offset: 0 };

/** 一行消息的几何量（相对滚动容器视口顶）：key 是服务端键，top/bottom 是这行的上下边。 */
export interface ScrollRow {
  key: string;
  top: number;
  bottom: number;
}

/** 视口顶部那条消息 → 内容锚：取**第一条「底边还在视口内」的行**——它就是用户此刻
 *  正在读的那条。几何量由调用方从 DOM 量好（本函数不碰 DOM，好断言）；
 *  一行都量不到（骨架期 / 空列表）时退回贴底锚。 */
export function anchorAtViewportTop(rows: readonly ScrollRow[]): ScrollAnchor {
  for (const row of rows) {
    if (row.bottom > 0) return { kind: "message", key: row.key, offset: row.top };
  }
  return BOTTOM_ANCHOR;
}

interface ScrollContext {
  /** 本页面生命里还没提交过任何权威内容（＝刷新、或首次进入这一页） */
  newPageLife: boolean;
  /** 用户在本页面生命里手动上翻过历史（stickToBottom === false） */
  userScrolledUp: boolean;
  /** 这次变化是「空间切换」（切走再切回同一个空间也算），不是首屏定界 */
  workspaceSwitched: boolean;
  /** 该空间在本页面生命里留下过锚点 */
  hasAnchor: boolean;
  /** 刚刚从骨架态切到真实内容（viewReady false → true 的那一次提交） */
  skeletonToContent: boolean;
}

/** 判据（自上而下第一条命中即返回）：
 *  ① 空间切换：这是用户的显式动作，还原该空间留下的锚（贴底锚继续贴底、内容锚钉回
 *     那条消息，「切走时在上方看历史」与「切走时贴着底部」都能还原）；没有锚就落到
 *     最新消息。
 *  ② 骨架 → 真实内容：列表高度在这一刻从 0 变成满屏，必须重新贴底——「刷新后停在
 *     最上方」的直接成因就是这一步没重滚。
 *  ③ 新页面生命（刷新/首次进入）：上一页面生命的锚不适用（几何已变、位置是上一页的），
 *     默认贴底——最新消息才是要看的东西。
 *  ④ 用户在本页面生命里手动上翻过：保位，任何 hydrate / 新消息都不得强行拉底。
 *  ⑤ 其余（跟随底部时的普通提交）：继续跟随底部。
 */
export function decideScroll(ctx: ScrollContext): ScrollDecision {
  if (ctx.workspaceSwitched) return ctx.hasAnchor ? "anchor" : "bottom";
  if (ctx.skeletonToContent) return "bottom";
  if (ctx.newPageLife) return "bottom";
  if (ctx.userScrolledUp) return "hold";
  return "bottom";
}
