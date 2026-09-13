package federation

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/service"
)

// TransactionRequest mirrors /send request.
type TransactionRequest struct {
	Origin         string            `json:"origin"`
	OriginServerTS int64             `json:"origin_server_ts"`
	TransactionsID string            `json:"transaction_id,omitempty"`
	PreviousIDs    []string          `json:"previous_ids,omitempty"`
	PDUs           []json.RawMessage `json:"pdus"`
	EDUs           []json.RawMessage `json:"edus"`
}

// ReceiveTransaction handles incoming federation transactions.
func ReceiveTransaction(w http.ResponseWriter, r *http.Request) {
	if !service.Global().Config.AllowFederation {
		apiutil.WriteMatrixError(w, apiutil.ErrMForbidden, "Federation disabled", http.StatusForbidden)
		return
	}
	var req TransactionRequest
	if err := apiutil.ReadJSONBody(r, service.Global().Config.MaxRequestSize, &req); err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMBadJSON, err.Error(), http.StatusBadRequest)
		return
	}
	txnID := chi.URLParam(r, "txnId")
	resp, err := service.Global().Federation.ReceiveTransaction(txnID, req.PDUs)
	if err != nil {
		apiutil.WriteMatrixError(w, apiutil.ErrMUnknown, err.Error(), http.StatusInternalServerError)
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, resp)
}
