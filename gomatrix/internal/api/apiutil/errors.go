package apiutil

import (
	"encoding/json"
	"net/http"
)

// MatrixErrorCode represents a standard Matrix error code.
type MatrixErrorCode string

const (
	ErrMForbidden        MatrixErrorCode = "M_FORBIDDEN"
	ErrMUnknownToken     MatrixErrorCode = "M_UNKNOWN_TOKEN"
	ErrMUnknown          MatrixErrorCode = "M_UNKNOWN"
	ErrMBadJSON          MatrixErrorCode = "M_BAD_JSON"
	ErrMNotJSON          MatrixErrorCode = "M_NOT_JSON"
	ErrMNotFound         MatrixErrorCode = "M_NOT_FOUND"
	ErrMUserInUse        MatrixErrorCode = "M_USER_IN_USE"
	ErrMInvalidUsername  MatrixErrorCode = "M_INVALID_USERNAME"
	ErrMExclusive        MatrixErrorCode = "M_EXCLUSIVE"
	ErrMMissingToken     MatrixErrorCode = "M_MISSING_TOKEN"
	ErrMUnrecognized     MatrixErrorCode = "M_UNRECOGNIZED"
	ErrMTooLarge         MatrixErrorCode = "M_TOO_LARGE"
	ErrMNotYetUploaded   MatrixErrorCode = "M_NOT_YET_UPLOADED"
	ErrMRoomInUse        MatrixErrorCode = "M_ROOM_IN_USE"
	ErrMBadState         MatrixErrorCode = "M_BAD_STATE"
	ErrMUnknownRoom      MatrixErrorCode = "M_UNKNOWN_ROOM"
	ErrMInvalidRoomState MatrixErrorCode = "M_INVALID_ROOM_STATE"
	ErrMInvalidParam     MatrixErrorCode = "M_INVALID_PARAM"
	ErrMLimitExceeded    MatrixErrorCode = "M_LIMIT_EXCEEDED"
)

// MatrixError is the standard error response body.
type MatrixError struct {
	ErrCode string `json:"errcode"`
	Error   string `json:"error"`
}

// WriteMatrixError writes a Matrix error response.
func WriteMatrixError(w http.ResponseWriter, code MatrixErrorCode, message string, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(MatrixError{ErrCode: string(code), Error: message})
}

// WriteJSON writes a JSON response with the given status.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// WriteEmptyJSON writes an empty JSON object.
func WriteEmptyJSON(w http.ResponseWriter, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write([]byte("{}"))
}
