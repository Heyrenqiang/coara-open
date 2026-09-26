package client

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// GetDisplayName returns a user's display name.
func GetDisplayName(w http.ResponseWriter, r *http.Request) {
	userID := chi.URLParam(r, "userId")
	user, err := service.Global().Users.GetUser(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if user == nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Profile not found", http.StatusNotFound)
		return
	}
	dn := ""
	if user.DisplayName != nil {
		dn = *user.DisplayName
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{
		"displayname": dn,
	})
}

// SetDisplayName updates the authenticated user's display name.
func SetDisplayName(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	paramUserID := chi.URLParam(r, "userId")
	if paramUserID != userID {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Cannot edit another user's profile", http.StatusForbidden)
		return
	}
	var req struct {
		DisplayName string `json:"displayname"`
	}
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	// 只覆盖 displayname：更新前读当前 avatar，避免整行覆盖清空头像。
	current, err := service.Global().Users.GetUser(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	avatarURL := ""
	if current != nil && current.AvatarURL != nil {
		avatarURL = *current.AvatarURL
	}
	if err := service.Global().Users.UpdateProfile(userID, req.DisplayName, avatarURL); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}

// GetAvatarURL returns a user's avatar URL.
func GetAvatarURL(w http.ResponseWriter, r *http.Request) {
	userID := chi.URLParam(r, "userId")
	user, err := service.Global().Users.GetUser(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if user == nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Profile not found", http.StatusNotFound)
		return
	}
	av := ""
	if user.AvatarURL != nil {
		av = *user.AvatarURL
	}
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{
		"avatar_url": av,
	})
}

// SetAvatarURL updates the authenticated user's avatar URL.
func SetAvatarURL(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	paramUserID := chi.URLParam(r, "userId")
	if paramUserID != userID {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Cannot edit another user's profile", http.StatusForbidden)
		return
	}
	var req struct {
		AvatarURL string `json:"avatar_url"`
	}
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	// 只覆盖 avatar_url：更新前读当前 displayname，避免整行覆盖清空昵称。
	current, err := service.Global().Users.GetUser(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	displayName := ""
	if current != nil && current.DisplayName != nil {
		displayName = *current.DisplayName
	}
	if err := service.Global().Users.UpdateProfile(userID, displayName, req.AvatarURL); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}
