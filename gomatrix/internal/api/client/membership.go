package client

import (
	"net/http"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// requireJoined writes an error and returns false unless the authenticated
// user is a joined member of the room (read endpoints must not leak room
// data to non-members).
func requireJoined(w http.ResponseWriter, r *http.Request, roomID string) bool {
	userID := apiutil.UserIDFromContext(r.Context())
	joined, err := service.Global().Rooms.IsJoined(roomID, userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return false
	}
	if !joined {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "You are not a member of this room", http.StatusForbidden)
		return false
	}
	return true
}
