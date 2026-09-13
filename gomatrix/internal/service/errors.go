package service

import "errors"

// ErrInvalidSyncToken is returned when the since token is malformed.
var ErrInvalidSyncToken = errors.New("invalid sync token")
