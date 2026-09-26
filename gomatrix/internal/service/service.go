package service

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"sync"
	"time"

	"gomatrix/internal/config"
	"gomatrix/internal/db"
)

// Services aggregates all business-logic services.
type Services struct {
	Config *config.Config
	DB     *db.DB

	Users      *UserService
	Rooms      *RoomService
	Timeline   *TimelineService
	Sync       *SyncService
	Typing     *TypingService
	Media      *MediaService
	Federation *FederationService

	registrationTickets *RegistrationTickets
}

const registrationTicketTTL = 5 * time.Minute

// RegistrationTickets issues short-lived, single-use registration capabilities
// for locally rendered pairing QR codes. They never persist to configuration.
type RegistrationTickets struct {
	mu      sync.Mutex
	tickets map[string]time.Time
}

func newRegistrationTickets() *RegistrationTickets {
	return &RegistrationTickets{tickets: make(map[string]time.Time)}
}

func (t *RegistrationTickets) Issue() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", fmt.Errorf("generate registration ticket: %w", err)
	}
	token := base64.RawURLEncoding.EncodeToString(bytes)
	t.mu.Lock()
	defer t.mu.Unlock()
	now := time.Now()
	for candidate, expiry := range t.tickets {
		if !expiry.After(now) {
			delete(t.tickets, candidate)
		}
	}
	t.tickets[token] = now.Add(registrationTicketTTL)
	return token, nil
}

func (t *RegistrationTickets) Consume(token string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	expiresAt, ok := t.tickets[token]
	delete(t.tickets, token)
	return ok && expiresAt.After(time.Now())
}

var (
	global   *Services
	globalMu sync.RWMutex
)

// SetGlobal stores the global service registry. Later calls replace the
// registry: production calls this exactly once at startup, while tests
// install per-case fixtures.
func SetGlobal(s *Services) {
	globalMu.Lock()
	defer globalMu.Unlock()
	global = s
}

// Global returns the global service registry.
func Global() *Services {
	globalMu.RLock()
	defer globalMu.RUnlock()
	return global
}

// Build creates all services from config and database.
func Build(cfg *config.Config, database *db.DB) *Services {
	s := &Services{
		Config: cfg,
		DB:     database,
	}
	s.Users = NewUserService(s)
	s.Rooms = NewRoomService(s)
	s.Timeline = NewTimelineService(s)
	s.Sync = NewSyncService(s)
	s.Typing = NewTypingService()
	s.Media = NewMediaService(s)
	s.Federation = NewFederationService(s)
	s.registrationTickets = newRegistrationTickets()
	return s
}

func (s *Services) IssueRegistrationTicket() (string, error) {
	return s.registrationTickets.Issue()
}

func (s *Services) ConsumeRegistrationTicket(token string) bool {
	return s.registrationTickets.Consume(token)
}
