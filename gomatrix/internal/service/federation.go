package service

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"

	"gomatrix/internal/models"
	"gomatrix/internal/utils"
)

// FederationService handles outbound/inbound federation stubs.
type FederationService struct {
	s         *Services
	keyPair   ed25519.PrivateKey
	publicKey ed25519.PublicKey
}

// NewFederationService creates the federation service and generates server keys.
func NewFederationService(s *Services) *FederationService {
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		panic(err)
	}
	return &FederationService{
		s:         s,
		keyPair:   priv,
		publicKey: priv.Public().(ed25519.PublicKey),
	}
}

// ServerKeyResponse is the JSON structure for /_matrix/key/v2/server.
type ServerKeyResponse struct {
	ServerName    string                       `json:"server_name"`
	VerifyKeys    map[string]map[string]string `json:"verify_keys"`
	OldVerifyKeys map[string]map[string]any    `json:"old_verify_keys"`
	ValidUntilTS  int64                        `json:"valid_until_ts"`
	Signatures    map[string]map[string]string `json:"signatures"`
}

// ServerKeys returns the server key document.
func (fs *FederationService) ServerKeys() *ServerKeyResponse {
	keyID := "ed25519:a_" + utils.GenerateMediaID()[:6]
	resp := &ServerKeyResponse{
		ServerName: fs.s.Config.ServerName,
		VerifyKeys: map[string]map[string]string{
			keyID: {
				"key": base64.RawStdEncoding.EncodeToString(fs.publicKey),
			},
		},
		OldVerifyKeys: map[string]map[string]any{},
		ValidUntilTS:  utils.NowMillis() + 7*24*60*60*1000,
		Signatures:    map[string]map[string]string{},
	}
	return resp
}

// ReceiveTransaction handles incoming federation /send.
func (fs *FederationService) ReceiveTransaction(txnID string, pdus []json.RawMessage) (*FederationSendResponse, error) {
	resp := &FederationSendResponse{
		PDUResults: make(map[string]json.RawMessage),
	}
	for _, raw := range pdus {
		var ev models.Event
		if err := json.Unmarshal(raw, &ev); err != nil {
			continue
		}
		// Basic validation
		if ev.RoomID == "" || ev.EventID == "" || ev.Sender == "" {
			continue
		}
		room, err := fs.s.DB.GetRoom(ev.RoomID)
		if err != nil || room == nil {
			resp.PDUResults[ev.EventID] = json.RawMessage(`{"error":"room not found"}`)
			continue
		}
		// Store as timeline event without deep auth checks
		_, err = fs.s.Timeline.createEvent(nil, ev.RoomID, ev.Sender, ev.Type, ev.StateKey, ev.Content, ev.Redacts)
		if err != nil {
			raw, merr := json.Marshal(map[string]string{"error": err.Error()})
			if merr != nil {
				raw = json.RawMessage(`{"error":"failed to store event"}`)
			}
			resp.PDUResults[ev.EventID] = raw
			continue
		}
		resp.PDUResults[ev.EventID] = json.RawMessage(`{}`)
	}
	return resp, nil
}

// FederationSendResponse mirrors the response for /send.
type FederationSendResponse struct {
	PDUResults map[string]json.RawMessage `json:"pdus"`
}

// PublicKey returns the base64-encoded public key.
func (fs *FederationService) PublicKey() string {
	return base64.RawStdEncoding.EncodeToString(fs.publicKey)
}
