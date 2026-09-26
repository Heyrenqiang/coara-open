/**
 * Orthogonal channel bundling for workflow canvas edges.
 *
 * Nearby parallel segments (same orientation, within ``grid``) snap onto a
 * shared corridor so N co-routed edges draw as one stroke until they fork —
 * classic channel / corridor routing, not spaced-apart parallel ribbons.
 */

export type Pt = { x: number; y: number };

export type EdgeSeed = {
  id: string;
  /** Orthogonal control polyline in flow coordinates (includes endpoints). */
  points: Pt[];
  /** Soft: prefer not to move these points (handle anchors). */
  pinnedEnds?: boolean;
};

export type BundleOptions = {
  /** Quantization for corridor assignment (px). */
  grid?: number;
  /** Max distance to join into the same corridor (px). */
  joinTol?: number;
};

type SegKind = 'h' | 'v';

type SegRef = {
  edgeId: string;
  /** Index of the segment start point in the edge polyline. */
  i0: number;
  kind: SegKind;
  /** Coordinate of the axis (y for H, x for V). */
  axis: number;
  /** Inclusive range on the free axis. */
  a0: number;
  a1: number;
};

function quantize(v: number, grid: number): number {
  return Math.round(v / grid) * grid;
}

function overlapLength(a0: number, a1: number, b0: number, b1: number): number {
  const loA = Math.min(a0, a1);
  const hiA = Math.max(a0, a1);
  const loB = Math.min(b0, b1);
  const hiB = Math.max(b0, b1);
  return Math.max(0, Math.min(hiA, hiB) - Math.max(loA, loB));
}

function expandOrth(points: Pt[]): Pt[] {
  if (points.length < 2) return points.slice();
  const out: Pt[] = [{ ...points[0] }];
  for (let i = 1; i < points.length; i++) {
    const a = out[out.length - 1];
    const b = points[i];
    if (Math.abs(a.x - b.x) < 0.5 || Math.abs(a.y - b.y) < 0.5) {
      out.push({ ...b });
    } else {
      // Prefer H then V (stable for bundling).
      out.push({ x: b.x, y: a.y });
      out.push({ ...b });
    }
  }
  return dedupePts(out);
}

function dedupePts(points: Pt[]): Pt[] {
  const out: Pt[] = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (last && Math.abs(last.x - p.x) < 0.5 && Math.abs(last.y - p.y) < 0.5) continue;
    out.push(p);
  }
  return out;
}

function collectSegments(edgeId: string, points: Pt[]): SegRef[] {
  const segs: SegRef[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    if (Math.abs(a.y - b.y) < 0.5) {
      segs.push({
        edgeId,
        i0: i,
        kind: 'h',
        axis: a.y,
        a0: a.x,
        a1: b.x,
      });
    } else if (Math.abs(a.x - b.x) < 0.5) {
      segs.push({
        edgeId,
        i0: i,
        kind: 'v',
        axis: a.x,
        a0: a.y,
        a1: b.y,
      });
    }
  }
  return segs;
}

/**
 * Union-find clusters of segments that share a corridor.
 * Same orientation, quantized axis within joinTol, overlapping free-range.
 */
function clusterSegments(segs: SegRef[], joinTol: number, grid: number): SegRef[][] {
  const n = segs.length;
  const parent = Array.from({ length: n }, (_, i) => i);
  const find = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]];
      i = parent[i];
    }
    return i;
  };
  const unite = (a: number, b: number) => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent[rb] = ra;
  };

  // Axis near + meaningful free-range overlap (not mere endpoint touch,
  // which would collapse an L-bend onto a neighbour's corridor).
  const minOverlap = Math.max(grid, 8);

  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const A = segs[i];
      const B = segs[j];
      if (A.kind !== B.kind) continue;
      if (A.edgeId === B.edgeId) continue;
      if (Math.abs(A.axis - B.axis) > joinTol) continue;
      if (overlapLength(A.a0, A.a1, B.a0, B.a1) < minOverlap) continue;
      unite(i, j);
    }
  }

  const groups = new Map<number, SegRef[]>();
  for (let i = 0; i < n; i++) {
    const r = find(i);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r)!.push(segs[i]);
  }
  return [...groups.values()];
}

/**
 * Snap co-routed orthogonal segments onto shared channel coordinates.
 * Returns a map edgeId → bundled polyline points.
 *
 * Seeds marked ``pinnedEnds`` keep their first/last segment axis untouched
 * so anchors stay glued to the port handles (only inner legs join corridors).
 */
export function bundleOrthogonalRoutes(
  seeds: EdgeSeed[],
  options: BundleOptions = {},
): Map<string, Pt[]> {
  const grid = options.grid ?? 12;
  const joinTol = options.joinTol ?? 18;

  const expanded = new Map<string, Pt[]>();
  const pinned = new Map<string, boolean>();
  for (const s of seeds) {
    expanded.set(s.id, expandOrth(s.points));
    pinned.set(s.id, Boolean(s.pinnedEnds));
  }

  const allSegs: SegRef[] = [];
  for (const [id, pts] of expanded) {
    allSegs.push(...collectSegments(id, pts));
  }

  // Process H and V separately so corridor axes don't mix.
  for (const kind of ['h', 'v'] as SegKind[]) {
    const kindSegs = allSegs.filter((s) => s.kind === kind);
    const clusters = clusterSegments(kindSegs, joinTol, grid);
    for (const cluster of clusters) {
      if (cluster.length < 2) continue;
      // Shared corridor = median axis (robust) then quantize for pixel-perfect overlap.
      const axes = cluster.map((s) => s.axis).sort((a, b) => a - b);
      const mid = axes[Math.floor(axes.length / 2)];
      const shared = quantize(mid, grid);

      for (const seg of cluster) {
        const pts = expanded.get(seg.edgeId);
        if (!pts) continue;
        // Anchored first/last legs never leave their port handles.
        const isEndSeg = seg.i0 === 0 || seg.i0 >= pts.length - 2;
        if (pinned.get(seg.edgeId) && isEndSeg) continue;
        const a = pts[seg.i0];
        const b = pts[seg.i0 + 1];
        if (!a || !b) continue;
        if (kind === 'h') {
          a.y = shared;
          b.y = shared;
        } else {
          a.x = shared;
          b.x = shared;
        }
      }
    }
  }

  // After axis snaps, re-expand to heal broken orth corners, then dedupe.
  const result = new Map<string, Pt[]>();
  for (const [id, pts] of expanded) {
    result.set(id, dedupePts(pts));
  }
  return result;
}

/** Expand diagonal pairs into H-then-V corners and dedupe (route healing). */
export function expandOrthRoute(points: Pt[]): Pt[] {
  return dedupePts(expandOrth(points));
}

/**
 * Translate both endpoints of segment `seg` perpendicular by `delta`
 * (rectangle-border parallel drag). Neighbouring segments stretch through
 * the shared endpoints; run `expandOrthRoute` afterwards to heal corners.
 */
export function translateOrthSegment(points: Pt[], seg: number, delta: number): Pt[] {
  const a = points[seg];
  const b = points[seg + 1];
  if (!a || !b) return points.slice();
  const horizontal = Math.abs(a.y - b.y) < 0.5;
  return points.map((pt, i) => {
    if (i !== seg && i !== seg + 1) return { ...pt };
    return horizontal ? { x: pt.x, y: pt.y + delta } : { x: pt.x + delta, y: pt.y };
  });
}

/** Build default orth polyline between two ports (H-V-H or V-H-V by delta). */
export function defaultOrthPoints(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  waypoints: Pt[] = [],
): Pt[] {
  if (waypoints.length > 0) {
    return expandOrth([{ x: sourceX, y: sourceY }, ...waypoints, { x: targetX, y: targetY }]);
  }
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  if (Math.abs(dx) < 1 && Math.abs(dy) < 1) {
    return [
      { x: sourceX, y: sourceY },
      { x: targetX, y: targetY },
    ];
  }
  // Mid-channel: for left→right flow use vertical corridor at midpoint x.
  if (Math.abs(dx) >= Math.abs(dy)) {
    const midX = quantize(sourceX + dx / 2, 12);
    return dedupePts([
      { x: sourceX, y: sourceY },
      { x: midX, y: sourceY },
      { x: midX, y: targetY },
      { x: targetX, y: targetY },
    ]);
  }
  const midY = quantize(sourceY + dy / 2, 12);
  return dedupePts([
    { x: sourceX, y: sourceY },
    { x: sourceX, y: midY },
    { x: targetX, y: midY },
    { x: targetX, y: targetY },
  ]);
}

/** SVG path from polyline. */
export function pointsToPath(points: Pt[]): string {
  if (points.length < 2) return '';
  const [first, ...rest] = points;
  let d = `M ${first.x},${first.y}`;
  for (const p of rest) d += ` L ${p.x},${p.y}`;
  return d;
}

/** Fixed-length port stub: edges leave/enter handles horizontally before turning. */
export const EDGE_STUB = 20;

/**
 * Data-edge routing (workflow best practice): a horizontal stub out of the
 * source port, a horizontal stub into the target port, and an orthogonal
 * corridor (plus user waypoints) in between. Backward references — target
 * left of source, e.g. reading an earlier step's output — wrap in a C-shape
 * instead of collapsing the stubs into each other.
 */
export function dataOrthPoints(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  waypoints: Pt[] = [],
): Pt[] {
  const sx = sourceX + EDGE_STUB;
  const tx = targetX - EDGE_STUB;
  if (waypoints.length > 0) {
    return dedupePts(
      expandOrth([
        { x: sourceX, y: sourceY },
        { x: sx, y: sourceY },
        ...waypoints,
        { x: tx, y: targetY },
        { x: targetX, y: targetY },
      ]),
    );
  }
  if (tx >= sx) {
    const midX = quantize(sx + (tx - sx) / 2, 12);
    return dedupePts([
      { x: sourceX, y: sourceY },
      { x: sx, y: sourceY },
      { x: midX, y: sourceY },
      { x: midX, y: targetY },
      { x: tx, y: targetY },
      { x: targetX, y: targetY },
    ]);
  }
  const midY = quantize(sourceY + (targetY - sourceY) / 2, 12);
  return dedupePts([
    { x: sourceX, y: sourceY },
    { x: sx, y: sourceY },
    { x: sx, y: midY },
    { x: tx, y: midY },
    { x: tx, y: targetY },
    { x: targetX, y: targetY },
  ]);
}

/** Remove intermediate points that sit collinear between their neighbours. */
function stripCollinear(points: Pt[]): Pt[] {
  const flat = (v: number): boolean => Math.abs(v) <= 0.5;
  const between = (v: number, a: number, b: number): boolean =>
    v >= Math.min(a, b) - 0.5 && v <= Math.max(a, b) + 0.5;
  const out: Pt[] = [];
  for (const p of points) {
    out.push(p);
    while (out.length >= 3) {
      const a = out[out.length - 3];
      const b = out[out.length - 2];
      const c = out[out.length - 1];
      const colH = flat(a.y - b.y) && flat(b.y - c.y) && between(b.x, a.x, c.x);
      const colV = flat(a.x - b.x) && flat(b.x - c.x) && between(b.y, a.y, c.y);
      if (!colH && !colV) break;
      out.splice(out.length - 2, 1);
    }
  }
  return out;
}

/**
 * Drop waypoints that do not affect the routed geometry. If removing a
 * waypoint leaves the orthogonal route identical (collinear clutter, e.g. a
 * bend dragged back onto the straight corridor), it is deleted. Iterates to
 * a fixpoint: one removal can make a neighbour redundant too.
 */
export function pruneRedundantWaypoints(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  waypoints: Pt[],
): Pt[] {
  const routeKey = (wps: Pt[]): string =>
    stripCollinear(dataOrthPoints(sourceX, sourceY, targetX, targetY, wps))
      .map((p) => `${Math.round(p.x)},${Math.round(p.y)}`)
      .join('|');
  let wps = waypoints.map((p) => ({ ...p }));
  let changed = true;
  while (changed && wps.length > 0) {
    changed = false;
    const full = routeKey(wps);
    for (let i = 0; i < wps.length; i++) {
      const trial = [...wps.slice(0, i), ...wps.slice(i + 1)];
      if (routeKey(trial) === full) {
        wps = trial;
        changed = true;
        break;
      }
    }
  }
  return wps;
}

/**
 * SVG path with rounded corners (smoothstep feel) from an orthogonal
 * polyline. Corner radius clamps to half of either adjacent segment so
 * short stubs never overshoot.
 */
export function pointsToRoundedPath(points: Pt[], radius = 10): string {
  const pts = dedupePts(points);
  if (pts.length < 3) return pointsToPath(pts);
  let d = `M ${pts[0].x},${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const prev = pts[i - 1];
    const cur = pts[i];
    const next = pts[i + 1];
    const inLen = Math.hypot(cur.x - prev.x, cur.y - prev.y);
    const outLen = Math.hypot(next.x - cur.x, next.y - cur.y);
    const r = Math.min(radius, inLen / 2, outLen / 2);
    if (r < 0.5) {
      d += ` L ${cur.x},${cur.y}`;
      continue;
    }
    const inX = (cur.x - prev.x) / inLen;
    const inY = (cur.y - prev.y) / inLen;
    const outX = (next.x - cur.x) / outLen;
    const outY = (next.y - cur.y) / outLen;
    const ax = cur.x - inX * r;
    const ay = cur.y - inY * r;
    const bx = cur.x + outX * r;
    const by = cur.y + outY * r;
    d += ` L ${ax},${ay} Q ${cur.x},${cur.y} ${bx},${by}`;
  }
  const last = pts[pts.length - 1];
  d += ` L ${last.x},${last.y}`;
  return d;
}

