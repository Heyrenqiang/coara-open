package utils

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
	"time"
)

var (
	localpartRe = regexp.MustCompile(`^[a-z0-9_.=/-]+$`)
	roomIDRe    = regexp.MustCompile(`^![A-Za-z0-9._~!$&'()*+,;=/-]+:`)
	roomAliasRe = regexp.MustCompile(`^#[A-Za-z0-9._~!$&'()*+,;=/-]+:`)
)

// GenerateToken creates a URL-safe random token.
func GenerateToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		// Entropy failure is unrecoverable: a weak token is worse than a crash.
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// GenerateDeviceID creates a random device identifier.
func GenerateDeviceID() string {
	b := make([]byte, 10)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// GenerateRoomID creates a new room ID for the given server name.
func GenerateRoomID(serverName string) string {
	b := make([]byte, 18)
	if _, err := rand.Read(b); err != nil {
		// Entropy failure is unrecoverable: a weak/zero ID risks collisions and
		// misrouted events. Match GenerateToken's policy — crash loud.
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}
	return "!" + base64.RawURLEncoding.EncodeToString(b) + ":" + serverName
}

// GenerateEventID creates a new event ID for the given server name.
func GenerateEventID(serverName string) string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}
	return "$" + hex.EncodeToString(b) + ":" + serverName
}

// GenerateMediaID creates a media ID.
func GenerateMediaID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand failed: %v", err))
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// ValidateLocalpart checks a Matrix localpart.
func ValidateLocalpart(localpart string) bool {
	if localpart == "" {
		return false
	}
	return localpartRe.MatchString(localpart)
}

// ValidateUserID checks that id looks like @localpart:server.
func ValidateUserID(id, serverName string) bool {
	if !strings.HasPrefix(id, "@") {
		return false
	}
	parts := strings.SplitN(id[1:], ":", 2)
	if len(parts) != 2 {
		return false
	}
	if parts[1] != serverName {
		return false
	}
	return ValidateLocalpart(parts[0])
}

// UserID constructs @localpart:server.
func UserID(localpart, serverName string) string {
	return "@" + localpart + ":" + serverName
}

// ExtractLocalpart returns the localpart from a user ID or the input itself.
func ExtractLocalpart(input, serverName string) string {
	if strings.HasPrefix(input, "@") {
		parts := strings.SplitN(input[1:], ":", 2)
		if len(parts) == 2 && parts[1] == serverName {
			return parts[0]
		}
	}
	return input
}

// ValidateRoomID validates a room ID.
func ValidateRoomID(id string) bool {
	return roomIDRe.MatchString(id)
}

// ValidateRoomAlias validates a room alias.
func ValidateRoomAlias(alias string) bool {
	return roomAliasRe.MatchString(alias)
}

// ParseRoomAlias extracts localpart and server name.
func ParseRoomAlias(alias string) (localpart, serverName string, ok bool) {
	if !strings.HasPrefix(alias, "#") {
		return "", "", false
	}
	parts := strings.SplitN(alias[1:], ":", 2)
	if len(parts) != 2 {
		return "", "", false
	}
	return parts[0], parts[1], true
}

// NowMillis returns the current time in milliseconds since epoch.
func NowMillis() int64 {
	return time.Now().UnixMilli()
}

// NowMillisTime returns time.Now().
func NowMillisTime() time.Time {
	return time.Now()
}

// DefaultIfEmpty returns value if non-empty, otherwise fallback.
func DefaultIfEmpty(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

// Ptr returns a pointer to the provided string.
func Ptr(s string) *string {
	return &s
}

// ContentTypeForFile guesses a content type from a filename extension.
func ContentTypeForFile(name string) string {
	ext := strings.ToLower(name)
	switch {
	case strings.HasSuffix(ext, ".png"):
		return "image/png"
	case strings.HasSuffix(ext, ".jpg"), strings.HasSuffix(ext, ".jpeg"):
		return "image/jpeg"
	case strings.HasSuffix(ext, ".gif"):
		return "image/gif"
	case strings.HasSuffix(ext, ".webp"):
		return "image/webp"
	case strings.HasSuffix(ext, ".mp4"):
		return "video/mp4"
	case strings.HasSuffix(ext, ".webm"):
		return "video/webm"
	case strings.HasSuffix(ext, ".ogg"):
		return "audio/ogg"
	case strings.HasSuffix(ext, ".pdf"):
		return "application/pdf"
	case strings.HasSuffix(ext, ".txt"):
		return "text/plain"
	default:
		return "application/octet-stream"
	}
}

// ContentDisposition returns an attachment disposition header value.
func ContentDisposition(filename string) string {
	return fmt.Sprintf("inline; filename=\"%s\"", strings.ReplaceAll(filename, "\"", "\\\""))
}
