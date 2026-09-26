/**
 * Quick sanity checks for channel bundling.
 * Run: npx tsx src/features/workflow/editor/edges/channel-bundle.selftest.ts
 * (also wired as `npm run test:edges`)
 */
import {
  bundleOrthogonalRoutes,
  dataOrthPoints,
  defaultOrthPoints,
  expandOrthRoute,
  pointsToPath,
  pointsToRoundedPath,
  pruneRedundantWaypoints,
  translateOrthSegment,
  EDGE_STUB,
} from './channel-bundle';

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

function near(a: number, b: number, tol = 0.5): boolean {
  return Math.abs(a - b) <= tol;
}

// Two near-parallel L→R edges: horizontal corridors must collapse to one y.
const a = defaultOrthPoints(0, 0, 200, 40);
const b = defaultOrthPoints(0, 14, 200, 54);
const bundled = bundleOrthogonalRoutes(
  [
    { id: 'a', points: a },
    { id: 'b', points: b },
  ],
  { grid: 12, joinTol: 20 },
);
const pa = bundled.get('a')!;
const pb = bundled.get('b')!;

function vertXs(pts: { x: number; y: number }[]): number[] {
  const xs: number[] = [];
  for (let i = 0; i < pts.length - 1; i++) {
    if (near(pts[i].x, pts[i + 1].x) && !near(pts[i].y, pts[i + 1].y)) {
      xs.push(pts[i].x);
    }
  }
  return xs;
}

const va = vertXs(pa);
const vb = vertXs(pb);
assert(va.length && vb.length, `expected vertical legs got ${va} / ${vb}`);
assert(near(va[0], vb[0]), `corridor x should match: ${va[0]} vs ${vb[0]}`);

// Shared bus merge: vertical at x=100 must stay shared.
const m1 = [
  { x: 10, y: 20 },
  { x: 100, y: 20 },
  { x: 100, y: 60 },
  { x: 120, y: 60 },
];
const m2 = [
  { x: 10, y: 80 },
  { x: 100, y: 80 },
  { x: 100, y: 60 },
  { x: 120, y: 60 },
];
const mb = bundleOrthogonalRoutes(
  [
    { id: 'm1', points: m1 },
    { id: 'm2', points: m2 },
  ],
  { grid: 12, joinTol: 20 },
);
const v1 = vertXs(mb.get('m1')!);
const v2 = vertXs(mb.get('m2')!);
assert(v1.length && v2.length, 'merge bus verticals');
assert(near(v1[0], v2[0]), `merge bus x shared: ${v1[0]} vs ${v2[0]}`);
assert(pointsToPath(mb.get('m1')!).startsWith('M '), 'path ok');

// pinnedEnds: anchored first/last legs must not join corridors.
const s1 = [
  { x: 0, y: 0 },
  { x: 200, y: 0 },
];
const s2 = [
  { x: 0, y: 10 },
  { x: 200, y: 10 },
];
const pb2 = bundleOrthogonalRoutes(
  [
    { id: 's1', points: s1, pinnedEnds: true },
    { id: 's2', points: s2, pinnedEnds: true },
  ],
  { grid: 12, joinTol: 20 },
);
assert(near(pb2.get('s1')![0].y, 0), `pinned start anchor moved: ${pb2.get('s1')![0].y}`);
assert(near(pb2.get('s1')![1].y, 0), `pinned end anchor moved: ${pb2.get('s1')![1].y}`);
assert(near(pb2.get('s2')![0].y, 10), 'pinned s2 anchor moved');

// pinnedEnds still bundles inner legs.
const mp = bundleOrthogonalRoutes(
  [
    { id: 'm1', points: m1, pinnedEnds: true },
    { id: 'm2', points: m2, pinnedEnds: true },
  ],
  { grid: 12, joinTol: 20 },
);
const pv1 = vertXs(mp.get('m1')!);
const pv2 = vertXs(mp.get('m2')!);
assert(pv1.length && pv2.length, 'pinned merge bus verticals');
assert(near(pv1[0], pv2[0]), `pinned merge bus x shared: ${pv1[0]} vs ${pv2[0]}`);
assert(near(mp.get('m1')![0].x, 10) && near(mp.get('m1')![0].y, 20), 'pinned m1 anchor kept');

// dataOrthPoints forward: horizontal stubs at both ends.
const fwd = dataOrthPoints(0, 0, 200, 40);
assert(near(fwd[0].x, 0) && near(fwd[0].y, 0), 'starts at source');
assert(near(fwd[1].x, EDGE_STUB) && near(fwd[1].y, 0), 'exit stub horizontal');
assert(
  near(fwd[fwd.length - 2].x, 200 - EDGE_STUB) && near(fwd[fwd.length - 2].y, 40),
  'entry stub horizontal',
);
assert(near(fwd[fwd.length - 1].x, 200) && near(fwd[fwd.length - 1].y, 40), 'ends at target');

// dataOrthPoints backward (target left of source): C-route keeps stubs.
const back = dataOrthPoints(200, 0, 0, 100);
assert(near(back[1].x, 200 + EDGE_STUB) && near(back[1].y, 0), 'backward exit stub');
assert(
  near(back[back.length - 2].x, -EDGE_STUB) && near(back[back.length - 2].y, 100),
  'backward entry stub',
);

// dataOrthPoints with waypoints: stubs still first/last, wp in between.
const wp = dataOrthPoints(0, 0, 200, 0, [{ x: 100, y: 60 }]);
assert(near(wp[1].x, EDGE_STUB) && near(wp[1].y, 0), 'wp route exit stub');
assert(
  near(wp[wp.length - 2].x, 200 - EDGE_STUB) && near(wp[wp.length - 2].y, 0),
  'wp route entry stub',
);
assert(wp.some((p) => near(p.x, 100) && near(p.y, 60)), 'waypoint kept in route');

// Rounded path: quadratic corners, clamped radius, degenerate fallback.
const rounded = pointsToRoundedPath(fwd, 10);
assert(rounded.startsWith('M '), 'rounded starts');
assert(rounded.includes(' Q '), 'rounded has corners');
assert(!rounded.includes('NaN'), 'rounded no NaN');
const twoPt = pointsToRoundedPath(
  [
    { x: 0, y: 0 },
    { x: 50, y: 0 },
  ],
  10,
);
assert(!twoPt.includes(' Q '), 'two-point path stays straight');

// pruneRedundantWaypoints: a bend dragged back onto the corridor disappears.
const prunedStraight = pruneRedundantWaypoints(0, 0, 400, 0, [{ x: 200, y: 0 }]);
assert(
  prunedStraight.length === 0,
  `collinear waypoint must be pruned: ${JSON.stringify(prunedStraight)}`,
);

// A real bend (off-corridor) survives pruning.
const keptBend = pruneRedundantWaypoints(0, 0, 400, 0, [{ x: 200, y: 120 }]);
assert(keptBend.length === 1, `real bend must be kept: ${JSON.stringify(keptBend)}`);

// Fixpoint: among two bends only the redundant one is removed.
const mixed = pruneRedundantWaypoints(0, 0, 400, 0, [
  { x: 150, y: 0 },
  { x: 250, y: 90 },
]);
assert(
  mixed.length === 1 && near(mixed[0].y, 90),
  `only the redundant bend should be pruned: ${JSON.stringify(mixed)}`,
);

// A fold-back bend changes the route shape — never pruned as "collinear".
const fold = pruneRedundantWaypoints(0, 0, 400, 0, [
  { x: 300, y: 0 },
  { x: 100, y: 0 },
]);
assert(fold.length > 0, `fold-back bends change geometry: ${JSON.stringify(fold)}`);

// Segment drag (rectangle-border model): translate a segment, heal, derive
// waypoints from interior corners — dataOrthPoints must reproduce the healed
// route exactly (corner count preserved, no new bends).
function routeKey(pts: { x: number; y: number }[]): string {
  return pts.map((p) => `${Math.round(p.x)},${Math.round(p.y)}`).join('|');
}

const baseRoute = dataOrthPoints(0, 0, 400, 40);
// Vertical leg of the Z route (segment index 2) translated by dx=+60.
const vMoved = expandOrthRoute(translateOrthSegment(baseRoute, 2, 60));
const vWps = vMoved.slice(2, vMoved.length - 2);
assert(
  routeKey(dataOrthPoints(0, 0, 400, 40, vWps)) === routeKey(vMoved),
  `vertical leg translation must round-trip: ${routeKey(vMoved)}`,
);
assert(
  vMoved.length === baseRoute.length,
  `parallel drag must not add corners: ${vMoved.length} vs ${baseRoute.length}`,
);

// Horizontal corridor (segment index 1) translated by dy=+30.
const hMoved = expandOrthRoute(translateOrthSegment(baseRoute, 1, 30));
const hWps = hMoved.slice(2, hMoved.length - 2);
assert(
  routeKey(dataOrthPoints(0, 0, 400, 40, hWps)) === routeKey(hMoved),
  `horizontal corridor translation must round-trip: ${routeKey(hMoved)}`,
);

// Translating back to delta 0 → base route → all derived bends prune away.
const backToBase = expandOrthRoute(translateOrthSegment(baseRoute, 2, 0));
const backWps = backToBase.slice(2, backToBase.length - 2);
const prunedBack = pruneRedundantWaypoints(0, 0, 400, 40, backWps);
assert(
  prunedBack.length === 0,
  `returning to base must prune all bends: ${JSON.stringify(prunedBack)}`,
);

console.log('channel-bundle ok');
