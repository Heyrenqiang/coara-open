/**
 * Orthogonal data edge with channel-bundled, rounded path + draggable bends.
 *
 * Interaction model (rectangle-border metaphor):
 * - Drag a segment anywhere on the line → the whole segment translates
 *   perpendicular (parallel move, like dragging a rectangle border). No new
 *   bend is created; the corner count never changes.
 * - Drag a bend dot → free-form corner move (like a rectangle corner).
 *   Dragging it back onto the would-be route snaps it onto the line, and the
 *   commit prunes it — bends never get stuck.
 * - Double-click the line → explicitly add a bend.
 * - Click selects; a small threshold separates click from drag.
 * - On commit, bends that no longer change the geometry are pruned.
 *
 * Waypoints sync live via EdgeEditContext and persist to the WDL document
 * once the drag ends (commitWaypoints).
 */

import { memo, useCallback, useMemo, useRef } from 'react';
import {
  BaseEdge,
  EdgeLabelRenderer,
  useReactFlow,
  type Edge,
  type EdgeProps,
} from '@xyflow/react';
import { useEdgeRoute } from './EdgeRouteProvider';
import { useEdgeEdit } from './EdgeEditContext';
import {
  dataOrthPoints,
  expandOrthRoute,
  pointsToRoundedPath,
  pruneRedundantWaypoints,
  translateOrthSegment,
} from './channel-bundle';
import type { RoutableEdgeData, Waypoint } from './edge-types';

export type { RoutableEdgeData, Waypoint } from './edge-types';

/** Screen-px movement required before a press becomes a drag. */
const DRAG_THRESHOLD = 6;
/** Flow-px distance within which a bend-add reuses an existing waypoint. */
const WAYPOINT_SNAP_DIST = 22;
/** Segment translation below this snaps back to the base route (px). */
const SEGMENT_SNAP = 6;
/** Bend dragged within this distance of its would-be route snaps onto it (px). */
const BEND_ELIMINATE_SNAP = 8;
const CORNER_RADIUS = 10;

function clampWaypoint(p: Waypoint): Waypoint {
  return { x: Math.round(p.x), y: Math.round(p.y) };
}

function dist2(a: Waypoint, b: Waypoint): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return dx * dx + dy * dy;
}

function nearestWaypointIndex(waypoints: Waypoint[], p: Waypoint, maxDist: number): number {
  let best = -1;
  let bestD = maxDist * maxDist;
  for (let i = 0; i < waypoints.length; i++) {
    const d = dist2(waypoints[i], p);
    if (d <= bestD) {
      bestD = d;
      best = i;
    }
  }
  return best;
}

/** Distance from p to segment ab plus the normalized projection t. */
function projectToSegment(p: Waypoint, a: Waypoint, b: Waypoint): { d: number; t: number } {
  const vx = b.x - a.x;
  const vy = b.y - a.y;
  const len2 = vx * vx + vy * vy;
  const t = len2 < 1e-6 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * vx + (p.y - a.y) * vy) / len2));
  const cx = a.x + t * vx;
  const cy = a.y + t * vy;
  return { d: Math.hypot(p.x - cx, p.y - cy), t };
}

/** Arc-length position of a point projected onto the polyline. */
function arcLenAt(points: Waypoint[], p: Waypoint): number {
  let acc = 0;
  let bestD = Infinity;
  let bestLen = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    const segLen = Math.hypot(b.x - a.x, b.y - a.y);
    const { d, t } = projectToSegment(p, a, b);
    if (d < bestD) {
      bestD = d;
      bestLen = acc + segLen * t;
    }
    acc += segLen;
  }
  return bestLen;
}

/** Index of the polyline segment nearest to p. */
function nearestSegmentIndex(points: Waypoint[], p: Waypoint): number {
  let best = -1;
  let bestD = Infinity;
  for (let i = 0; i < points.length - 1; i++) {
    const { d } = projectToSegment(p, points[i], points[i + 1]);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  }
  return best;
}

/** Nearest point on the polyline to p, if within maxDist; otherwise p itself. */
function snapToPolyline(points: Waypoint[], p: Waypoint, maxDist: number): Waypoint {
  let best = p;
  let bestD = maxDist;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    const vx = b.x - a.x;
    const vy = b.y - a.y;
    const len2 = vx * vx + vy * vy;
    const t = len2 < 1e-6 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * vx + (p.y - a.y) * vy) / len2));
    const c = { x: a.x + t * vx, y: a.y + t * vy };
    const d = Math.hypot(p.x - c.x, p.y - c.y);
    if (d < bestD) {
      bestD = d;
      best = c;
    }
  }
  return best;
}

/** Arc-length midpoint of a polyline (stable label anchor for Z/C routes). */
function midPointOfPolyline(points: Waypoint[]): Waypoint {
  if (points.length < 2) return points[0] || { x: 0, y: 0 };
  let total = 0;
  for (let i = 0; i < points.length - 1; i++) {
    total += Math.hypot(points[i + 1].x - points[i].x, points[i + 1].y - points[i].y);
  }
  let acc = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    const segLen = Math.hypot(b.x - a.x, b.y - a.y);
    if (acc + segLen >= total / 2 && segLen > 0) {
      const t = (total / 2 - acc) / segLen;
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
    }
    acc += segLen;
  }
  return points[points.length - 1];
}

function RoutableEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  style,
  markerEnd,
  data,
  selected,
  label,
  labelStyle,
  labelShowBg,
  labelBgStyle,
  labelBgPadding,
  labelBgBorderRadius,
}: EdgeProps<Edge<RoutableEdgeData>>) {
  const { screenToFlowPosition } = useReactFlow();
  const { updateWaypoints, commitWaypoints } = useEdgeEdit();
  const waypoints = (data?.waypoints || []) as Waypoint[];
  const waypointsRef = useRef(waypoints);
  waypointsRef.current = waypoints;
  const routed = useEdgeRoute(id);

  /** Un-bundled seed route (port stubs + waypoints); also the render fallback. */
  const seedPoints = useMemo(
    () => dataOrthPoints(sourceX, sourceY, targetX, targetY, waypoints),
    [sourceX, sourceY, targetX, targetY, waypoints],
  );

  const path = useMemo(() => {
    if (routed?.path) return routed.path;
    return pointsToRoundedPath(seedPoints, CORNER_RADIUS);
  }, [routed, seedPoints]);

  const routePoints = useMemo(() => {
    if (routed?.points?.length) return routed.points;
    return seedPoints;
  }, [routed, seedPoints]);

  const labelPos = useMemo(() => midPointOfPolyline(routePoints), [routePoints]);

  /**
   * Commit a finished route: first drop bends that no longer change the
   * geometry (e.g. dragged back onto the straight corridor), then persist.
   * Keeps waypoints minimal instead of letting them accumulate.
   */
  const commitPruned = useCallback(
    (wps: Waypoint[]) => {
      const pruned = pruneRedundantWaypoints(sourceX, sourceY, targetX, targetY, wps);
      waypointsRef.current = pruned;
      updateWaypoints(id, pruned);
      commitWaypoints(id, pruned);
    },
    [id, sourceX, sourceY, targetX, targetY, updateWaypoints, commitWaypoints],
  );

  /**
   * Free-form bend move (rectangle-corner drag). Snaps onto the route the
   * edge would take without this bend, so dropping back onto the line makes
   * the bend exactly redundant and the commit prunes it away.
   */
  const moveWaypoint = useCallback(
    (index: number, raw: Waypoint) => {
      const cur = waypointsRef.current[index];
      if (!cur) return;
      const others = waypointsRef.current.filter((_, i) => i !== index);
      const without = dataOrthPoints(sourceX, sourceY, targetX, targetY, others);
      const pos = clampWaypoint(snapToPolyline(without, clampWaypoint(raw), BEND_ELIMINATE_SNAP));
      const next = waypointsRef.current.map((w, i) => (i === index ? pos : w));
      waypointsRef.current = next;
      updateWaypoints(id, next);
    },
    [id, sourceX, sourceY, targetX, targetY, updateWaypoints],
  );

  /**
   * Reuse a waypoint near pos, otherwise insert a new one in route order
   * (arc-length along the seed polyline) so the line never folds back.
   */
  const ensureWaypointAt = useCallback(
    (pos: Waypoint): number => {
      const cur = [...waypointsRef.current];
      const hit = nearestWaypointIndex(cur, pos, WAYPOINT_SNAP_DIST);
      if (hit >= 0) return hit;
      const route = dataOrthPoints(sourceX, sourceY, targetX, targetY, cur);
      const newLen = arcLenAt(route, pos);
      let idx = cur.findIndex((w) => arcLenAt(route, w) > newLen);
      if (idx < 0) idx = cur.length;
      const next = [...cur.slice(0, idx), pos, ...cur.slice(idx)];
      waypointsRef.current = next;
      updateWaypoints(id, next);
      return idx;
    },
    [id, updateWaypoints, sourceX, sourceY, targetX, targetY],
  );

  /**
   * Bend-dot drag lifecycle: threshold gating (click ≠ drag), live updates,
   * single pruned commit on pointerup.
   */
  const installDrag = useCallback(
    (
      pointerId: number,
      targetEl: Element,
      startIndex: number,
      startClient: { x: number; y: number },
    ) => {
      try {
        (targetEl as HTMLElement).setPointerCapture?.(pointerId);
      } catch {
        /* pointer already released or invalid — drag still works via window listeners */
      }
      let engaged = false;
      const move = (ev: PointerEvent) => {
        if (!engaged) {
          if (Math.hypot(ev.clientX - startClient.x, ev.clientY - startClient.y) < DRAG_THRESHOLD) {
            return;
          }
          engaged = true;
        }
        moveWaypoint(startIndex, screenToFlowPosition({ x: ev.clientX, y: ev.clientY }));
      };
      const up = (ev: PointerEvent) => {
        try {
          (targetEl as HTMLElement).releasePointerCapture?.(ev.pointerId);
        } catch {
          /* ignore */
        }
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        if (engaged) commitPruned(waypointsRef.current);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    },
    [moveWaypoint, commitPruned],
  );

  /**
   * Segment drag (rectangle-border drag): the whole segment translates
   * perpendicular. Works from a captured base route so live re-renders never
   * feed back into the drag math. Stubs (first/last segment) stay glued to
   * their ports and are not draggable. Corner count never changes; returning
   * the segment to its base position snaps to delta 0, and the commit prunes
   * any bend that became redundant.
   */
  const installSegmentDrag = useCallback(
    (pointerId: number, targetEl: Element, startClient: { x: number; y: number }) => {
      const pressFlow = screenToFlowPosition(startClient);
      const baseRoute = dataOrthPoints(sourceX, sourceY, targetX, targetY, waypointsRef.current);
      if (baseRoute.length < 4) return; // stubs only — nothing translatable
      const seg = nearestSegmentIndex(baseRoute, pressFlow);
      if (seg <= 0 || seg >= baseRoute.length - 2) return; // stubs are glued to ports
      try {
        (targetEl as HTMLElement).setPointerCapture?.(pointerId);
      } catch {
        /* ignore */
      }
      let engaged = false;
      const move = (ev: PointerEvent) => {
        if (!engaged) {
          if (Math.hypot(ev.clientX - startClient.x, ev.clientY - startClient.y) < DRAG_THRESHOLD) {
            return;
          }
          engaged = true;
        }
        const flow = screenToFlowPosition({ x: ev.clientX, y: ev.clientY });
        const a = baseRoute[seg];
        const b = baseRoute[seg + 1];
        const horizontal = Math.abs(a.y - b.y) < 0.5;
        let delta = horizontal ? flow.y - pressFlow.y : flow.x - pressFlow.x;
        if (Math.abs(delta) < SEGMENT_SNAP) delta = 0;
        const healed = expandOrthRoute(translateOrthSegment(baseRoute, seg, delta));
        // Interior corners (exclude endpoints + port stubs) are exactly the
        // waypoints that reproduce the healed route.
        const wps = healed.slice(2, healed.length - 2).map(clampWaypoint);
        waypointsRef.current = wps;
        updateWaypoints(id, wps);
      };
      const up = (ev: PointerEvent) => {
        try {
          (targetEl as HTMLElement).releasePointerCapture?.(ev.pointerId);
        } catch {
          /* ignore */
        }
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        if (engaged) commitPruned(waypointsRef.current);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    },
    [id, sourceX, sourceY, targetX, targetY, screenToFlowPosition, updateWaypoints, commitPruned],
  );

  const onPathPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      e.preventDefault();
      installSegmentDrag(e.pointerId, e.currentTarget, { x: e.clientX, y: e.clientY });
    },
    [installSegmentDrag],
  );

  /** Double-click explicitly adds a bend at that point. */
  const onPathDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      const pos = clampWaypoint(screenToFlowPosition({ x: e.clientX, y: e.clientY }));
      ensureWaypointAt(pos);
      commitWaypoints(id, waypointsRef.current);
    },
    [id, screenToFlowPosition, ensureWaypointAt, commitWaypoints],
  );

  const onWaypointPointerDown = useCallback(
    (index: number, e: React.PointerEvent) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      e.preventDefault();
      installDrag(e.pointerId, e.currentTarget, index, { x: e.clientX, y: e.clientY });
    },
    [installDrag],
  );

  const onWaypointDoubleClick = useCallback(
    (index: number, e: React.MouseEvent) => {
      e.stopPropagation();
      const next = waypointsRef.current.filter((_, i) => i !== index);
      commitPruned(next);
    },
    [commitPruned],
  );

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        style={style}
        markerEnd={markerEnd}
        interactionWidth={24}
        label={label}
        labelX={labelPos.x}
        labelY={labelPos.y}
        labelStyle={labelStyle}
        labelShowBg={labelShowBg}
        labelBgStyle={labelBgStyle}
        labelBgPadding={labelBgPadding}
        labelBgBorderRadius={labelBgBorderRadius}
      />
      {selected && (
        <>
          {/* Selection trace: soft glow + marching dashes flowing source→target,
              so a merged/bundled edge's full route is unmistakable. */}
          <path d={path} className="routable-edge-highlight" pointerEvents="none" />
          <path d={path} className="routable-edge-highlight-flow" pointerEvents="none" />
        </>
      )}
      <path
        d={path}
        fill="none"
        stroke="transparent"
        strokeWidth={24}
        className="routable-edge-hit nodrag nopan"
        onPointerDown={onPathPointerDown}
        onDoubleClick={onPathDoubleClick}
        style={{ cursor: 'grab', pointerEvents: 'stroke' }}
      />
      <EdgeLabelRenderer>
        {waypoints.map((wp, i) => (
          <div
            key={`wp-${id}-${i}`}
            className={`routable-waypoint nodrag nopan${selected ? ' selected' : ''}`}
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${wp.x}px,${wp.y}px)`,
              pointerEvents: 'all',
            }}
            onPointerDown={(e) => onWaypointPointerDown(i, e)}
            onDoubleClick={(e) => onWaypointDoubleClick(i, e)}
            title="拖拽调拐角；拖回线上松开即消除；双击删除"
          />
        ))}
        {selected && waypoints.length === 0 && (
          <div
            className="routable-edge-hint"
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelPos.x}px,${labelPos.y - 14}px)`,
              pointerEvents: 'none',
            }}
          >
            拖动平移线段 · 双击加拐点
          </div>
        )}
      </EdgeLabelRenderer>
    </>
  );
}

export const RoutableEdge = memo(RoutableEdgeComponent);
