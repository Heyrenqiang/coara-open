package client

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// DeviceListResponse mirrors /devices.
type DeviceListResponse struct {
	Devices []*deviceInfo `json:"devices"`
}

type deviceInfo struct {
	DeviceID    string `json:"device_id"`
	DisplayName string `json:"display_name,omitempty"`
	LastSeenIP  string `json:"last_seen_ip,omitempty"`
	LastSeenTs  int64  `json:"last_seen_ts,omitempty"`
}

// GetDevices lists devices.
func GetDevices(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	devices, err := service.Global().Users.GetDevices(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	resp := &DeviceListResponse{}
	for _, d := range devices {
		info := &deviceInfo{
			DeviceID:    d.DeviceID,
			DisplayName: d.DisplayName,
			LastSeenIP:  d.LastSeenIP,
		}
		if d.LastSeenAt.Valid {
			info.LastSeenTs = d.LastSeenAt.Time.UnixMilli()
		}
		resp.Devices = append(resp.Devices, info)
	}
	apiutil.WriteJSON(w, http.StatusOK, resp)
}

// UpdateDevice updates a device's display name.
func UpdateDevice(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	deviceID := chi.URLParam(r, "deviceId")
	var req struct {
		DisplayName string `json:"display_name"`
	}
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	dev, err := service.Global().Users.GetDevices(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	found := false
	for _, d := range dev {
		if d.DeviceID == deviceID {
			found = true
			break
		}
	}
	if !found {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Device not found", http.StatusNotFound)
		return
	}
	if err := service.Global().Users.UpdateDevice(deviceID, req.DisplayName); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}

// DeleteDevice deletes a device.
func DeleteDevice(w http.ResponseWriter, r *http.Request) {
	userID := apiutil.UserIDFromContext(r.Context())
	deviceID := chi.URLParam(r, "deviceId")
	dev, err := service.Global().Users.GetDevices(userID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	found := false
	for _, d := range dev {
		if d.DeviceID == deviceID {
			found = true
			break
		}
	}
	if !found {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Device not found", http.StatusNotFound)
		return
	}
	if err := service.Global().Users.DeleteDevice(deviceID); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteEmptyJSON(w, http.StatusOK)
}
