package client

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// GetState returns all state events for a room.
func GetState(w http.ResponseWriter, r *http.Request) {
	roomID := chi.URLParam(r, "roomId")
	if !requireJoined(w, r, roomID) {
		return
	}
	events, err := service.Global().Rooms.GetState(roomID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, events)
}

// GetStateEvent returns a single state event.
func GetStateEvent(w http.ResponseWriter, r *http.Request) {
	roomID := chi.URLParam(r, "roomId")
	if !requireJoined(w, r, roomID) {
		return
	}
	eventType := chi.URLParam(r, "eventType")
	stateKey := chi.URLParam(r, "stateKey")
	ev, err := service.Global().Rooms.GetStateEvent(roomID, eventType, stateKey)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if ev == nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Event not found", http.StatusNotFound)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, ev.Content)
}

// SetStateEvent sets a state event.
func SetStateEvent(w http.ResponseWriter, r *http.Request) {
	sender := apiutil.UserIDFromContext(r.Context())
	roomID := chi.URLParam(r, "roomId")
	eventType := chi.URLParam(r, "eventType")
	stateKey := chi.URLParam(r, "stateKey")

	var content json.RawMessage
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &content); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}

	ev, err := service.Global().Rooms.SetState(roomID, sender, eventType, stateKey, content)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, err.Error(), http.StatusForbidden)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{"event_id": ev.EventID})
}
