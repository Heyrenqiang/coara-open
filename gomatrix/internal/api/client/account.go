package client

import (
	"encoding/json"
	"net/http"

	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
	"gomatrix/internal/utils"
)

// RegisterRequest is the body of /register.
type RegisterRequest struct {
	Username                 string          `json:"username"`
	Password                 string          `json:"password"`
	DeviceID                 string          `json:"device_id"`
	InitialDeviceDisplayName string          `json:"initial_device_display_name"`
	Auth                     json.RawMessage `json:"auth"`
}

// RegisterResponse is the response of /register.
type RegisterResponse struct {
	UserID      string `json:"user_id"`
	AccessToken string `json:"access_token"`
	DeviceID    string `json:"device_id"`
	HomeServer  string `json:"home_server"`
}

// Register handles account registration.
func Register(w http.ResponseWriter, r *http.Request) {
	cfg := service.Global().Config
	if !cfg.AllowRegistration {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Registration is disabled", http.StatusForbidden)
		return
	}

	var req RegisterRequest
	if err := apiutil.ReadJSONBody(r, cfg.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}

	var auth struct {
		Type  string `json:"type"`
		Token string `json:"token"`
	}
	_ = json.Unmarshal(req.Auth, &auth)
	if auth.Type != "m.login.registration_token" || !service.Global().ConsumeRegistrationTicket(auth.Token) {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Invalid or expired registration ticket", http.StatusForbidden)
		return
	}

	if req.Username == "" || req.Password == "" {
		apiutil.WriteMatrixError(w, apiutil.ErrMInvalidUsername, "Username and password required", http.StatusBadRequest)
		return
	}

	// First registered user becomes admin.
	admin := isFirstUser()
	userID, err := service.Global().Users.Register(req.Username, req.Password, admin)
	if err != nil {
		if err.Error() == "user in use" {
			apiutil.WriteMatrixError(w, apiutil.ErrMUserInUse, "User ID already taken", http.StatusConflict)
			return
		}
		if err.Error() == "invalid localpart" {
			apiutil.WriteMatrixError(w, apiutil.ErrMInvalidUsername, "Invalid username", http.StatusBadRequest)
			return
		}
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}

	_, token, deviceID, err := service.Global().Users.Login(req.Username, req.Password, req.DeviceID, req.InitialDeviceDisplayName)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}

	apiutil.WriteJSON(w, http.StatusOK, RegisterResponse{
		UserID:      userID,
		AccessToken: token,
		DeviceID:    deviceID,
		HomeServer:  cfg.ServerName,
	})
}

// LoginRequest is the body of /login.
type LoginRequest struct {
	Type                     string          `json:"type"`
	User                     string          `json:"user"`
	Identifier               json.RawMessage `json:"identifier"`
	Password                 string          `json:"password"`
	DeviceID                 string          `json:"device_id"`
	InitialDeviceDisplayName string          `json:"initial_device_display_name"`
}

// Login handles login.
func Login(w http.ResponseWriter, r *http.Request) {
	cfg := service.Global().Config
	var req LoginRequest
	if err := apiutil.ReadJSONBody(r, cfg.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}

	localpart := req.User
	if localpart == "" && len(req.Identifier) > 0 {
		var id struct {
			Type string `json:"type"`
			User string `json:"user"`
		}
		_ = json.Unmarshal(req.Identifier, &id)
		if id.Type == "m.id.user" {
			localpart = id.User
		}
	}

	if localpart == "" || req.Password == "" {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "User and password required", http.StatusBadRequest)
		return
	}

	// Strip @localpart:server if provided
	localpart = utils.ExtractLocalpart(localpart, cfg.ServerName)

	userID, token, deviceID, err := service.Global().Users.Login(localpart, req.Password, req.DeviceID, req.InitialDeviceDisplayName)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Invalid username or password", http.StatusForbidden)
		return
	}

	apiutil.WriteJSON(w, http.StatusOK, RegisterResponse{
		UserID:      userID,
		AccessToken: token,
		DeviceID:    deviceID,
		HomeServer:  cfg.ServerName,
	})
}

// Logout handles logout.
func Logout(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("access_token")
	if token == "" {
		h := r.Header.Get("Authorization")
		if len(h) > 7 && h[:7] == "Bearer " {
			token = h[7:]
		}
	}
	if token != "" {
		userID, _, _ := service.Global().Users.GetUserByToken(token)
		_ = service.Global().Users.Logout(token, false, userID)
	}
	apiutil.WriteJSON(w, http.StatusOK, struct{}{})
}

// WhoamiResponse mirrors /account/whoami.
type WhoamiResponse struct {
	UserID   string `json:"user_id"`
	DeviceID string `json:"device_id"`
	IsGuest  bool   `json:"is_guest"`
}

// Whoami returns the authenticated user.
func Whoami(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	deviceID := apiutil.DeviceIDFromContext(r.Context())
	apiutil.WriteJSON(w, http.StatusOK, WhoamiResponse{UserID: userID, DeviceID: deviceID, IsGuest: false})
}

// Capabilities returns server capabilities.
func Capabilities(w http.ResponseWriter, r *http.Request) {
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{
		"capabilities": map[string]any{
			"m.room_versions": map[string]any{
				"default": service.Global().Config.DefaultRoomVersion,
				"available": map[string]string{
					"1":  "stable",
					"2":  "stable",
					"3":  "stable",
					"4":  "stable",
					"5":  "stable",
					"6":  "stable",
					"7":  "stable",
					"8":  "stable",
					"9":  "stable",
					"10": "stable",
				},
			},
			"m.change_password": map[string]bool{"enabled": false},
			"m.set_displayname": map[string]bool{"enabled": true},
			"m.set_avatar_url":  map[string]bool{"enabled": true},
		},
	})
}

// Versions returns supported spec versions.
func Versions(w http.ResponseWriter, r *http.Request) {
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{
		"versions": []string{
			"r0.0.1",
			"r0.1.0",
			"r0.2.0",
			"r0.3.0",
			"r0.4.0",
			"r0.5.0",
			"r0.6.0",
			"r0.6.1",
			"v1.1",
			"v1.2",
			"v1.3",
			"v1.4",
			"v1.5",
			"v1.6",
			"v1.7",
			"v1.8",
			"v1.9",
			"v1.10",
			"v1.11",
		},
		"unstable_features": map[string]bool{
			"org.matrix.msc3244.room_capabilities": false,
		},
	})
}

// WellKnown returns the client well-known.
// The base_url uses the request's Host header so clients (including the
// coara Android app) get a reachable URL rather than the internal server_name
// (e.g. "coara.local") which is not DNS-resolvable.
func WellKnown(w http.ResponseWriter, r *http.Request) {
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	// Honour X-Forwarded-Proto from reverse proxies (e.g. Cloudflare tunnel).
	if xfp := r.Header.Get("X-Forwarded-Proto"); xfp != "" {
		scheme = xfp
	}
	baseURL := scheme + "://" + r.Host
	apiutil.WriteJSON(w, http.StatusOK, map[string]any{
		"m.homeserver": map[string]string{
			"base_url": baseURL,
		},
	})
}

func isFirstUser() bool {
	n, err := service.Global().Users.UserCount()
	return err == nil && n == 0
}
