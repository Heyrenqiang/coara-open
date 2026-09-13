package apiutil

import (
	"context"
)

// ctxKey is a private type for context keys.
type ctxKey int

const (
	ctxUserID ctxKey = iota
	ctxDeviceID
)

// WithAuthUser returns a context with the authenticated user/device.
func WithAuthUser(ctx context.Context, userID, deviceID string) context.Context {
	ctx = context.WithValue(ctx, ctxUserID, userID)
	ctx = context.WithValue(ctx, ctxDeviceID, deviceID)
	return ctx
}

// UserIDFromContext extracts the authenticated user ID.
func UserIDFromContext(ctx context.Context) string {
	v, _ := ctx.Value(ctxUserID).(string)
	return v
}

// DeviceIDFromContext extracts the authenticated device ID.
func DeviceIDFromContext(ctx context.Context) string {
	v, _ := ctx.Value(ctxDeviceID).(string)
	return v
}
