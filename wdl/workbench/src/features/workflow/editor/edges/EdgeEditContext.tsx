/**
 * Bridge edge edits into WorkflowEditor's controlled ``edges`` state.
 * useReactFlow().setEdges alone is wiped by ``edges={displayEdges}``.
 */

import { createContext, useContext } from 'react';
import type { Waypoint } from './edge-types';

export type EdgeEditApi = {
  updateWaypoints: (edgeId: string, waypoints: Waypoint[]) => void;
  /** Persist the finished route into the WDL document (call on drag end). */
  commitWaypoints: (edgeId: string, waypoints: Waypoint[]) => void;
};

const EdgeEditContext = createContext<EdgeEditApi>({
  updateWaypoints: () => undefined,
  commitWaypoints: () => undefined,
});

export function EdgeEditProvider({
  value,
  children,
}: {
  value: EdgeEditApi;
  children: React.ReactNode;
}) {
  return <EdgeEditContext.Provider value={value}>{children}</EdgeEditContext.Provider>;
}

export function useEdgeEdit(): EdgeEditApi {
  return useContext(EdgeEditContext);
}
