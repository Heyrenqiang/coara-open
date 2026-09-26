package client

import (
	"encoding/json"
	"net/http"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// KeysUploadRequest is the body of /keys/upload.
type KeysUploadRequest struct {
	DeviceKeys   json.RawMessage            `json:"device_keys"`
	OneTimeKeys  map[string]json.RawMessage `json:"one_time_keys"`
	FallbackKeys map[string]json.RawMessage `json:"fallback_keys"`
}

// KeysUploadResponse returns counts.
type KeysUploadResponse struct {
	OneTimeKeyCounts map[string]int `json:"one_time_key_counts"`
}

// KeysUpload is a stub that accepts keys but does not implement real E2EE.
func KeysUpload(w http.ResponseWriter, r *http.Request) {
	var req KeysUploadRequest
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, KeysUploadResponse{
		OneTimeKeyCounts: map[string]int{},
	})
}

// KeysQueryRequest is the body of /keys/query.
type KeysQueryRequest struct {
	DeviceKeys map[string][]string `json:"device_keys"`
	Token      string              `json:"token"`
}

// KeysQueryResponse returns empty device keys.
type KeysQueryResponse struct {
	DeviceKeys map[string]map[string]json.RawMessage `json:"device_keys"`
	Failures   map[string]any                        `json:"failures"`
}

// KeysQuery is a stub.
func KeysQuery(w http.ResponseWriter, r *http.Request) {
	var req KeysQueryRequest
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, KeysQueryResponse{
		DeviceKeys: make(map[string]map[string]json.RawMessage),
		Failures:   make(map[string]any),
	})
}

// KeysClaimRequest is the body of /keys/claim.
type KeysClaimRequest struct {
	OneTimeKeys map[string]map[string]string `json:"one_time_keys"`
	Timeout     int                          `json:"timeout"`
}

// KeysClaimResponse returns empty one-time keys.
type KeysClaimResponse struct {
	OneTimeKeys map[string]map[string]json.RawMessage `json:"one_time_keys"`
	Failures    map[string]any                        `json:"failures"`
}

// KeysClaim is a stub.
func KeysClaim(w http.ResponseWriter, r *http.Request) {
	var req KeysClaimRequest
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, KeysClaimResponse{
		OneTimeKeys: make(map[string]map[string]json.RawMessage),
		Failures:    make(map[string]any),
	})
}
