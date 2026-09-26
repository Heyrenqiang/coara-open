package client

import (
	"encoding/json"
	"net/http"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// AgentsResponse is the JSON response for GET /api/agents.
type AgentsResponse struct {
	Agents []service.AgentInfo `json:"agents"`
}

// Agents returns the list of registered agents.
// This endpoint is unauthenticated so the Android app can discover available agents
// before logging in.
func Agents(w http.ResponseWriter, r *http.Request) {
	svc := service.Global()
	if svc == nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, "service not initialized", http.StatusInternalServerError)
		return
	}
	agents := service.ListAgents(svc)
	resp := AgentsResponse{Agents: agents}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}
