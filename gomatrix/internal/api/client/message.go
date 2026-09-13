package client

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/db"
	"gomatrix/internal/service"
)

func parseStreamToken(raw string) int64 {
	if raw == "" {
		return 0
	}
	if len(raw) > 1 && (raw[0] == 't' || raw[0] == 's') {
		raw = raw[1:]
	}
	n, _ := strconv.ParseInt(raw, 10, 64)
	return n
}

// GetMessages handles /rooms/{roomId}/messages.
func GetMessages(w http.ResponseWriter, r *http.Request) {
	roomID := chi.URLParam(r, "roomId")
	if !requireJoined(w, r, roomID) {
		return
	}
	fromStr := r.URL.Query().Get("from")
	toStr := r.URL.Query().Get("to")
	limitStr := r.URL.Query().Get("limit")
	dir := r.URL.Query().Get("dir")

	from := parseStreamToken(fromStr)
	to := parseStreamToken(toStr)
	limit, _ := strconv.Atoi(limitStr)
	if limit <= 0 {
		limit = 10
	}
	backward := dir != "f"

	events, start, end, err := service.Global().Timeline.GetMessages(roomID, from, to, limit, backward)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}

	apiutil.WriteJSON(w, http.StatusOK, map[string]any{
		"start": start,
		"end":   end,
		"chunk": events,
	})
}

// SendMessage handles PUT /rooms/{roomId}/send/{eventType}/{txnId}.
func SendMessage(w http.ResponseWriter, r *http.Request) {
	sender := apiutil.UserIDFromContext(r.Context())
	roomID := chi.URLParam(r, "roomId")
	eventType := chi.URLParam(r, "eventType")
	txnID := chi.URLParam(r, "txnId")

	var content json.RawMessage
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &content); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}

	ev, err := service.Global().Timeline.SendMessageWithTxn(roomID, sender, eventType, content, txnID)
	if err != nil {
		if db.IsBusy(err) {
			apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusServiceUnavailable)
			return
		}
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, err.Error(), http.StatusForbidden)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{"event_id": ev.EventID})
}
