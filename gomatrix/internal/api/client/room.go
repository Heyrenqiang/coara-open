package client

import (
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// CreateRoom handles POST /createRoom.
func CreateRoom(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	var req service.CreateRoomRequest
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}

	room, err := service.Global().Rooms.CreateRoom(userID, &req)
	if err != nil {
		if err.Error() == "room in use" || strings.Contains(err.Error(), "UNIQUE") {
			apiutil.WriteMatrixError(w, apiutil.ErrMRoomInUse, err.Error(), http.StatusConflict)
			return
		}
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{"room_id": room.RoomID})
}

// JoinRoom handles POST /join/{roomIdOrAlias}.
func JoinRoom(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	idOrAlias := chi.URLParam(r, "roomIdOrAlias")
	var roomID string
	if strings.HasPrefix(idOrAlias, "#") {
		resolved, err := service.Global().Rooms.ResolveAlias(idOrAlias)
		if err != nil {
			apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
			return
		}
		if resolved == "" {
			apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Room alias not found", http.StatusNotFound)
			return
		}
		roomID = resolved
	} else {
		roomID = idOrAlias
	}

	if err := service.Global().Rooms.JoinRoom(roomID, userID); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, err.Error(), http.StatusForbidden)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{"room_id": roomID})
}

// JoinRoomByID handles POST /rooms/{roomId}/join.
func JoinRoomByID(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	roomID := chi.URLParam(r, "roomId")
	if err := service.Global().Rooms.JoinRoom(roomID, userID); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, err.Error(), http.StatusForbidden)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{"room_id": roomID})
}

// Invite handles POST /rooms/{roomId}/invite.
func Invite(w http.ResponseWriter, r *http.Request) {
	inviter := apiutil.UserIDFromContext(r.Context())
	roomID := chi.URLParam(r, "roomId")
	var req struct {
		UserID string `json:"user_id"`
	}
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	if req.UserID == "" {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, "user_id required", http.StatusBadRequest)
		return
	}
	if err := service.Global().Rooms.InviteUser(roomID, inviter, req.UserID); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, err.Error(), http.StatusForbidden)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}

// Leave handles POST /rooms/{roomId}/leave.
func Leave(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	roomID := chi.URLParam(r, "roomId")
	if err := service.Global().Rooms.LeaveRoom(roomID, userID); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, err.Error(), http.StatusForbidden)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}

// Forget handles POST /rooms/{roomId}/forget.
func Forget(w http.ResponseWriter, r *http.Request) {
	// For minimal server, forget is a no-op.
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}

// ResolveAlias handles GET /directory/room/{roomAlias}.
func ResolveAlias(w http.ResponseWriter, r *http.Request) {
	alias := "#" + chi.URLParam(r, "roomAlias")
	roomID, err := service.Global().Rooms.ResolveAlias(alias)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if roomID == "" {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Room alias not found", http.StatusNotFound)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{
		"room_id": roomID,
		"servers": []string{service.Global().Config.ServerName},
	})
}

// SetAlias handles PUT /directory/room/{roomAlias}.
func SetAlias(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	alias := "#" + chi.URLParam(r, "roomAlias")
	var req struct {
		RoomID string `json:"room_id"`
	}
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	room, err := service.Global().Rooms.GetRoom(req.RoomID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if room == nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Room not found", http.StatusNotFound)
		return
	}
	if room.Creator != userID {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Only room creator can set alias", http.StatusForbidden)
		return
	}
	if err := service.Global().DB.CreateAlias(alias, req.RoomID, userID); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}

// DeleteAlias handles DELETE /directory/room/{roomAlias}.
func DeleteAlias(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	alias := "#" + chi.URLParam(r, "roomAlias")
	roomID, creator, err := service.Global().DB.GetAlias(alias)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if roomID != "" && creator != userID {
		// Also allow the room creator, consistent with SetAlias.
		room, err := service.Global().Rooms.GetRoom(roomID)
		if err != nil {
			apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
			return
		}
		if room == nil || room.Creator != userID {
			apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Only the alias or room creator can delete this alias", http.StatusForbidden)
			return
		}
	}
	if err := service.Global().DB.DeleteAlias(alias); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}
