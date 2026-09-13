package client

import (
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
	"gomatrix/internal/utils"
)

// isActiveContentType reports content types that a browser would execute or
// render as active content (stored-XSS risk); they must be downloaded as
// attachments instead of being displayed inline.
func isActiveContentType(contentType string) bool {
	ct := strings.ToLower(strings.TrimSpace(strings.SplitN(contentType, ";", 2)[0]))
	switch ct {
	case "image/svg+xml", "text/html", "application/xhtml+xml", "text/xml", "application/xml":
		return true
	}
	return false
}

// UploadMedia handles POST /media/v3/upload.
func UploadMedia(w http.ResponseWriter, r *http.Request) {
	filename := r.URL.Query().Get("filename")
	contentType := r.Header.Get("Content-Type")
	maxSize := service.Global().Config.MaxRequestSize
	data, err := io.ReadAll(io.LimitReader(r.Body, maxSize+1))
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	if int64(len(data)) > maxSize {
		apiutil.WriteMatrixError(w, apiutil.ErrMTooLarge, "Upload too large", http.StatusRequestEntityTooLarge)
		return
	}

	media, err := service.Global().Media.Upload(data, contentType, filename)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}

	mxc := "mxc://" + service.Global().Config.ServerName + "/" + media.MediaID
	apiutil.WriteJSON(w, http.StatusOK, map[string]string{
		"content_uri": mxc,
	})
}

func writeMediaDownload(w http.ResponseWriter, r *http.Request, serverName, mediaID string) {
	if serverName != service.Global().Config.ServerName {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, "Remote media not supported", http.StatusNotFound)
		return
	}
	media, data, err := service.Global().Media.GetMedia(mediaID)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMNotFound, err.Error(), http.StatusNotFound)
		return
	}
	filename := r.URL.Query().Get("filename")
	if filename == "" {
		filename = media.MediaID
	}
	w.Header().Set("Content-Type", media.ContentType)
	w.Header().Set("X-Content-Type-Options", "nosniff")
	disposition := utils.ContentDisposition(filename)
	if isActiveContentType(media.ContentType) {
		disposition = "attachment" + disposition[len("inline"):]
	}
	w.Header().Set("Content-Disposition", disposition)
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

// DownloadMedia handles GET /media/v3/download/{serverName}/{mediaId}.
func DownloadMedia(w http.ResponseWriter, r *http.Request) {
	writeMediaDownload(w, r, chi.URLParam(r, "serverName"), chi.URLParam(r, "mediaId"))
}

// ClientDownloadMedia handles GET /client/v1/media/download/{serverName}/{mediaId} (matrix-nio + coara App).
func ClientDownloadMedia(w http.ResponseWriter, r *http.Request) {
	writeMediaDownload(w, r, chi.URLParam(r, "serverName"), chi.URLParam(r, "mediaId"))
}

// ClientThumbnailMedia handles GET /client/v1/media/thumbnail/{serverName}/{mediaId}.
func ClientThumbnailMedia(w http.ResponseWriter, r *http.Request) {
	writeMediaDownload(w, r, chi.URLParam(r, "serverName"), chi.URLParam(r, "mediaId"))
}
