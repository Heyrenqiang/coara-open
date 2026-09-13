package client

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// Sync handles GET /sync.
func Sync(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	deviceID := apiutil.DeviceIDFromContext(r.Context())
	since := r.URL.Query().Get("since")
	timeoutStr := r.URL.Query().Get("timeout")
	timeoutMS, _ := strconv.ParseInt(timeoutStr, 10, 64)
	if timeoutMS < 0 {
		timeoutMS = 0
	}
	if timeoutMS > 30_000 {
		timeoutMS = 30_000
	}

	resp, err := service.Global().Sync.Sync(
		r.Context(),
		userID,
		deviceID,
		since,
		time.Duration(timeoutMS)*time.Millisecond,
	)
	if err != nil {
		if errors.Is(err, service.ErrInvalidSyncToken) {
			apiutil.WriteMatrixError(w, apiutil.ErrMInvalidParam, err.Error(), http.StatusBadRequest)
			return
		}
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, resp)
}
