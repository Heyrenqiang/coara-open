package client

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

type typingBody struct {
	Typing  bool  `json:"typing"`
	Timeout int64 `json:"timeout"`
}

// SetTyping handles PUT /rooms/{roomId}/typing/{userId}.
func SetTyping(w http.ResponseWriter, r *http.Request) {
	roomID := chi.URLParam(r, "roomId")
	targetUser := chi.URLParam(r, "userId")
	authUser := apiutil.UserIDFromContext(r.Context())
	if targetUser != authUser {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Cannot set typing for another user", http.StatusForbidden)
		return
	}
	// 与同类端点一致：仅房间成员可上报 typing 状态。
	if !requireJoined(w, r, roomID) {
		return
	}

	var body typingBody
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &body); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}

	service.Global().Typing.SetTyping(roomID, authUser, body.Typing, body.Timeout)
	service.Global().Sync.NotifyRoomEphemeral(roomID)
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}
