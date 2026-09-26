/**
 * Global edge-route provider: seeds every edge polyline, runs channel bundling,
 * exposes SVG paths so co-routed edges share corridors (strong merge feel).
 */

import {
  createContext,
  useContext,
  useMemo,
  type ReactNode,
} from 'react';
import {
  useStore,
  type Edge,
  type InternalNode,
  type Node,
  type ReactFlowState,
} from '@xyflow/react';
import {
  bundleOrthogonalRoutes,
  dataOrthPoints,
  pointsToRoundedPath,
  type EdgeSeed,
  type Pt,
} from './channel-bundle';
import type { RoutableEdgeData } from './edge-types';

export type EdgeRouteMap = Map<string, { path: string; points: Pt[] }>;

const EdgeRouteContext = createContext<EdgeRouteMap>(new Map());

export function useEdgeRoute(edgeId: string): { path: string; points: Pt[] } | null {
  const map = useContext(EdgeRouteContext);
  return map.get(edgeId) ?? null;
}

type HandleBox = { id: string | null; x: number; y: number; width: number; height: number };

function handleCenter(
  node: InternalNode<Node> | undefined,
  handleId: string | null | undefined,
  side: 'source' | 'target',
): Pt | null {
  if (!node) return null;
  const abs = node.internals.positionAbsolute;
  const bounds = node.internals.handleBounds?.[side] as HandleBox[] | undefined;
  const w = node.measured?.width ?? (node as { width?: number }).width ?? 0;
  const h = node.measured?.height ?? (node as { height?: number }).height ?? 0;
  if (!bounds?.length) {
    // Fallback: mid of left/right edge
    if (side === 'source') return { x: abs.x + w, y: abs.y + h / 2 };
    return { x: abs.x, y: abs.y + h / 2 };
  }
  const wanted = handleId || null;
  const hit =
    bounds.find((b) => (b.id || null) === wanted) ||
    bounds.find((b) => !wanted && side === 'source' && (b.id === 'exec-out' || b.id?.startsWith('out:'))) ||
    bounds[0];
  if (!hit) return null;
  return {
    x: abs.x + hit.x + hit.width / 2,
    y: abs.y + hit.y + hit.height / 2,
  };
}

function selectEdges(s: ReactFlowState): Edge[] {
  return s.edges;
}

function selectNodeLookup(s: ReactFlowState): Map<string, InternalNode<Node>> {
  return s.nodeLookup as Map<string, InternalNode<Node>>;
}

/** Must render inside <ReactFlow>. */
export function EdgeRouteProvider({ children }: { children: ReactNode }) {
  const edges = useStore(selectEdges);
  const nodeLookup = useStore(selectNodeLookup);
  // Recompute when node positions / sizes change.
  const positionKey = useStore((s) =>
    s.nodes
      .map((n) => {
        const abs = (s.nodeLookup.get(n.id) as InternalNode<Node> | undefined)?.internals
          .positionAbsolute;
        const m = (s.nodeLookup.get(n.id) as InternalNode<Node> | undefined)?.measured;
        return `${n.id}:${abs?.x ?? 0},${abs?.y ?? 0},${m?.width ?? 0}x${m?.height ?? 0}`;
      })
      .join('|'),
  );

  const routes = useMemo(() => {
    const seeds: EdgeSeed[] = [];
    const dataEdgeIds = new Set<string>();
    // Edges with user-dragged waypoints are explicit manual routes: they skip
    // channel bundling so the rendered line always honors the user's bends.
    const direct = new Map<string, { path: string; points: Pt[] }>();

    for (const edge of edges) {
      // Kernel edges stay as default bezier curves — skip channel bundling.
      if (edge.type === 'default' || edge.className === 'edge-exec') continue;

      const src = handleCenter(
        nodeLookup.get(edge.source),
        edge.sourceHandle,
        'source',
      );
      const tgt = handleCenter(
        nodeLookup.get(edge.target),
        edge.targetHandle,
        'target',
      );
      if (!src || !tgt) continue;

      const data = (edge.data || {}) as RoutableEdgeData;
      const waypoints = Array.isArray(data.waypoints) ? (data.waypoints as Pt[]) : [];

      if (edge.type === 'routable' || edge.className === 'edge-data-flow') {
        dataEdgeIds.add(edge.id);
        // Port stubs + corridor; rounded corners applied below.
        const points = dataOrthPoints(src.x, src.y, tgt.x, tgt.y, waypoints);
        if (waypoints.length > 0) {
          direct.set(edge.id, { path: pointsToRoundedPath(points, 10), points });
          continue;
        }
        seeds.push({ id: edge.id, points, pinnedEnds: true });
      }
    }

    const bundled = bundleOrthogonalRoutes(seeds, { grid: 12, joinTol: 20 });
    const map: EdgeRouteMap = new Map();
    for (const [id, points] of bundled) {
      map.set(id, { path: pointsToRoundedPath(points, 10), points });
    }
    for (const [id, route] of direct) {
      map.set(id, route);
    }
    return map;
    // positionKey captures geometry churn
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edges, nodeLookup, positionKey]);

  return <EdgeRouteContext.Provider value={routes}>{children}</EdgeRouteContext.Provider>;
}
