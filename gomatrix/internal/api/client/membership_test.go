package client

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
	"gomatrix/internal/api/apiutil"
	"gomatrix/internal/config"
	"gomatrix/internal/db"
	"gomatrix/internal/service"
)

// Shared fixtures: the whole package shares a single service registry
// (built here in TestMain).
var (
	testRouter http.Handler
	testRoomID string
	testIDs    map[string]string
)

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "gomatrix-client-test")
	if err != nil {
		panic(err)
	}
	cfg := &config.Config{
		ServerName:         "test.local",
		DatabasePath:       filepath.Join(dir, "test.db"),
		Address:            "127.0.0.1",
		Port:               0,
		MaxRequestSize:     1024 * 1024,
		AllowRegistration:  true,
		DefaultRoomVersion: "10",
		MediaPath:          filepath.Join(dir, "media"),
		LogLevel:           "error",
	}
	database, err := db.Open(cfg)
	if err != nil {
		panic(err)
	}
	s := service.Build(cfg, database)
	service.SetGlobal(s)

	aliceID, err := s.Users.Register("alice", "secret", false)
	if err != nil {
		panic(err)
	}
	bobID, err := s.Users.Register("bob", "secret", false)
	if err != nil {
		panic(err)
	}
	charlieID, err := s.Users.Register("charlie", "secret", false)
	if err != nil {
		panic(err)
	}
	room, err := s.Rooms.CreateRoom(aliceID, &service.CreateRoomRequest{
		Name:   "Guarded",
		Invite: []string{bobID},
	})
	if err != nil {
		panic(err)
	}
	if err := s.Rooms.JoinRoom(room.RoomID, bobID); err != nil {
		panic(err)
	}

	r := chi.NewRouter()
	r.Get("/rooms/{roomId}/messages", GetMessages)
	r.Get("/rooms/{roomId}/state", GetState)
	r.Get("/rooms/{roomId}/state/{eventType}/{stateKey}", GetStateEvent)
	r.Put("/rooms/{roomId}/state/{eventType}/{stateKey}", SetStateEvent)
	r.Get("/rooms/{roomId}/members", RoomMembers)

	testRouter = r
	testRoomID = room.RoomID
	testIDs = map[string]string{
		"creator":  aliceID,
		"member":   bobID,
		"outsider": charlieID,
	}

	code := m.Run()
	database.Close()
	os.RemoveAll(dir)
	os.Exit(code)
}

func doRequest(t *testing.T, method, path, userID, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req = req.WithContext(apiutil.WithAuthUser(req.Context(), userID, "dev"))
	rr := httptest.NewRecorder()
	testRouter.ServeHTTP(rr, req)
	return rr
}

func TestReadEndpointsRequireJoinedMembership(t *testing.T) {
	// wantMember is the status a joined member gets; the outsider must always
	// get 403 before any existence check leaks information.
	cases := []struct {
		path       string
		wantMember int
	}{
		{"/rooms/" + testRoomID + "/messages", http.StatusOK},
		{"/rooms/" + testRoomID + "/state", http.StatusOK},
		{"/rooms/" + testRoomID + "/state/m.room.name/main", http.StatusNotFound}, // state key "main" does not exist
		{"/rooms/" + testRoomID + "/members", http.StatusOK},
	}
	for _, tc := range cases {
		for role, userID := range testIDs {
			rr := doRequest(t, http.MethodGet, tc.path, userID, "")
			want := tc.wantMember
			if role == "outsider" {
				want = http.StatusForbidden
			}
			if rr.Code != want {
				t.Errorf("GET %s as %s: status = %d, want %d (body: %s)", tc.path, role, rr.Code, want, rr.Body.String())
			}
			if role == "outsider" && !strings.Contains(rr.Body.String(), "M_FORBIDDEN") {
				t.Errorf("GET %s as outsider: body %q missing M_FORBIDDEN", tc.path, rr.Body.String())
			}
		}
	}
}

func TestSetStateEventPowerLevels(t *testing.T) {
	cases := []struct {
		name      string
		userID    string
		eventType string
		want      int
	}{
		// Creator (power 100) may set everything.
		{"creator sets name", testIDs["creator"], "m.room.name", http.StatusOK},
		{"creator sets power_levels", testIDs["creator"], "m.room.power_levels", http.StatusOK},
		{"creator sets custom event", testIDs["creator"], "com.example.custom", http.StatusOK},
		// Plain member (power 0) is below state_default=50 and per-event levels.
		{"member cannot set name", testIDs["member"], "m.room.name", http.StatusForbidden},
		{"member cannot set power_levels", testIDs["member"], "m.room.power_levels", http.StatusForbidden},
		{"member cannot set custom event", testIDs["member"], "com.example.custom", http.StatusForbidden},
		// Non-member is rejected regardless of power levels.
		{"outsider cannot set name", testIDs["outsider"], "m.room.name", http.StatusForbidden},
	}
	for _, tc := range cases {
		path := "/rooms/" + testRoomID + "/state/" + tc.eventType + "/main"
		rr := doRequest(t, http.MethodPut, path, tc.userID, `{"name":"x"}`)
		if rr.Code != tc.want {
			t.Errorf("%s: PUT %s status = %d, want %d (body: %s)", tc.name, path, rr.Code, tc.want, rr.Body.String())
		}
	}
}
