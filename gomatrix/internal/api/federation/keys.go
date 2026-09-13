package federation

import (
	"fmt"
	"net/http"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// ServerKeys returns the server key document.
func ServerKeys(w http.ResponseWriter, r *http.Request) {
	apiutil.WriteJSON(w, http.StatusOK, service.Global().Federation.ServerKeys())
}

// WellKnown returns the server well-known.
func WellKnown(w http.ResponseWriter, r *http.Request) {
	cfg := service.Global().Config
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{
		"m.server": fmt.Sprintf("%s:%d", cfg.ServerName, cfg.Port),
	})
}
