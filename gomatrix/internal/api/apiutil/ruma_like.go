package apiutil

import (
	"encoding/json"
	"io"
	"net/http"
)

// ReadJSONBody reads and unmarshals the request body.
func ReadJSONBody(r *http.Request, maxSize int64, v any) error {
	if maxSize <= 0 {
		maxSize = 1024 * 1024
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxSize+1))
	if err != nil {
		return err
	}
	if int64(len(body)) > maxSize {
		return &BodyTooLargeError{}
	}
	if len(body) == 0 {
		return nil
	}
	return json.Unmarshal(body, v)
}

// BodyTooLargeError indicates the request body exceeded the limit.
type BodyTooLargeError struct{}

func (e *BodyTooLargeError) Error() string { return "request body too large" }


