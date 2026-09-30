/** Shared edge data shapes for routable edges. */

export type Waypoint = { x: number; y: number };

export type RoutableEdgeData = {
  waypoints?: Waypoint[];
  /** 数据绑定边：完整模板引用（inputs.x / steps.y.text） */
  ref?: string;
  /** 数据绑定边：写入 input 的 {{…}} 模板 */
  template?: string;
  /** 拓扑边：success | error */
  on?: string;
  [key: string]: unknown;
};
