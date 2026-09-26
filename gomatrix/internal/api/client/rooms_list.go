package client

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// JoinedRooms handles GET /joined_rooms.
func JoinedRooms(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	roomIDs, err := service.Global().DB.GetJoinedRoomsForUser(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if roomIDs == nil {
		roomIDs = []string{}
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{"joined_rooms": roomIDs})
}

// RoomMembers handles GET /rooms/{roomId}/members (minimal chunk for the coara App).
func RoomMembers(w http.ResponseWriter, r *http.Request) {
	roomID := chi.URLParam(r, "roomId")
	if !requireJoined(w, r, roomID) {
		return
	}
	members, err := service.Global().DB.GetRoomMembers(roomID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	chunk := make([]map[string]any, 0, len(members))
	for _, m := range members {
		userID := m.UserID
		content, _ := json.Marshal(map[string]string{"membership": m.Membership})
		chunk = append(chunk, map[string]any{
			"type":      "m.room.member",
			"state_key": userID,
			"sender":    userID,
			"content":   json.RawMessage(content),
		})
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{"chunk": chunk})
}
