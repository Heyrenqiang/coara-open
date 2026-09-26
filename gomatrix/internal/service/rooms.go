package service

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

	"gomatrix/internal/models"
	"gomatrix/internal/utils"
)

// RoomService handles room lifecycle and membership.
type RoomService struct {
	s *Services
}

// NewRoomService creates a room service.
func NewRoomService(s *Services) *RoomService {
	return &RoomService{s: s}
}

// CreateRoom creates a room with the given creator and initial options.
func (rs *RoomService) CreateRoom(creator string, req *CreateRoomRequest) (*models.Room, error) {
	roomID := utils.GenerateRoomID(rs.s.Config.ServerName)
	room := &models.Room{
		RoomID:    roomID,
		Version:   utils.DefaultIfEmpty(req.RoomVersion, rs.s.Config.DefaultRoomVersion),
		Creator:   creator,
		JoinRules: "invite",
		CreatedAt: utils.NowMillisTime(),
	}

	err := rs.s.DB.InTransaction(func(tx *sql.Tx) error {
		_, err := tx.Exec(
			`INSERT INTO rooms (room_id, version, creator, name, topic, canonical_alias, encrypted, join_rules, created_at)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			room.RoomID, room.Version, room.Creator, room.Name, room.Topic, room.CanonicalAlias,
			room.Encrypted, room.JoinRules, room.CreatedAt.UnixMilli(),
		)
		if err != nil {
			return err
		}

		// m.room.create
		createContent, _ := json.Marshal(map[string]any{
			"creator":      creator,
			"room_version": room.Version,
		})
		if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.create", "", createContent, nil); err != nil {
			return err
		}

		// m.room.member creator join
		memberContent, _ := json.Marshal(map[string]any{
			"membership": "join",
		})
		if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.member", creator, memberContent, nil); err != nil {
			return err
		}

		// m.room.power_levels
		plContent, _ := json.Marshal(defaultPowerLevels(creator))
		if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.power_levels", "", plContent, nil); err != nil {
			return err
		}

		// m.room.join_rules
		if req.Preset == "public_chat" {
			room.JoinRules = "public"
		}
		jrContent, _ := json.Marshal(map[string]any{"join_rule": room.JoinRules})
		if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.join_rules", "", jrContent, nil); err != nil {
			return err
		}

		// m.room.name
		if req.Name != "" {
			nameContent, _ := json.Marshal(map[string]any{"name": req.Name})
			if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.name", "", nameContent, nil); err != nil {
				return err
			}
			room.Name = &req.Name
		}

		// m.room.topic
		if req.Topic != "" {
			topicContent, _ := json.Marshal(map[string]any{"topic": req.Topic})
			if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.topic", "", topicContent, nil); err != nil {
				return err
			}
			room.Topic = &req.Topic
		}

		// m.room.canonical_alias
		if req.RoomAliasName != "" {
			alias := "#" + req.RoomAliasName + ":" + rs.s.Config.ServerName
			if _, err := tx.Exec(
				`INSERT INTO room_aliases (alias, room_id, creator) VALUES (?, ?, ?)`,
				alias, roomID, creator,
			); err != nil {
				return err
			}
			caContent, _ := json.Marshal(map[string]any{"alias": alias})
			if _, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, creator, "m.room.canonical_alias", "", caContent, nil); err != nil {
				return err
			}
			room.CanonicalAlias = &alias
			_, _ = tx.Exec(`UPDATE rooms SET canonical_alias = ? WHERE room_id = ?`, alias, roomID)
		}

		// Invite initial users
		for _, userID := range req.Invite {
			if err := rs.inviteUserTx(tx, roomID, creator, userID); err != nil {
				return err
			}
		}

		return nil
	})
	if err != nil {
		return nil, err
	}

	rs.s.Sync.Notify(roomID, 0)

	// Re-fetch room to reflect canonical alias
	return rs.s.DB.GetRoom(roomID)
}

// CreateRoomRequest mirrors the createRoom request body.
type CreateRoomRequest struct {
	Preset        string   `json:"preset"`
	Name          string   `json:"name"`
	Topic         string   `json:"topic"`
	RoomAliasName string   `json:"room_alias_name"`
	RoomVersion   string   `json:"room_version"`
	Invite        []string `json:"invite"`
	InitialState  []struct {
		Type     string          `json:"type"`
		StateKey string          `json:"state_key"`
		Content  json.RawMessage `json:"content"`
	} `json:"initial_state"`
	CreationContent           json.RawMessage `json:"creation_content"`
	PowerLevelContentOverride json.RawMessage `json:"power_level_content_override"`
}

// JoinRoom makes a user join a room.
func (rs *RoomService) JoinRoom(roomID, userID string) error {
	room, err := rs.s.DB.GetRoom(roomID)
	if err != nil {
		return err
	}
	if room == nil {
		return fmt.Errorf("room not found")
	}
	membership, err := rs.s.DB.GetMembership(roomID, userID)
	if err != nil {
		return err
	}
	if membership == models.MembershipJoin {
		return nil
	}
	if membership == models.MembershipBan {
		return fmt.Errorf("user is banned")
	}
	if room.JoinRules != "public" && membership != models.MembershipInvite {
		if !rs.hasInviteState(roomID, userID) {
			return fmt.Errorf("not invited")
		}
	}
	content, _ := json.Marshal(map[string]any{"membership": "join"})
	_, err = rs.s.Timeline.createStateEvent(nil, roomID, userID, "m.room.member", userID, content, nil)
	return err
}

// InviteUser invites a user to a room.
func (rs *RoomService) InviteUser(roomID, inviter, invitee string) error {
	err := rs.s.DB.InTransaction(func(tx *sql.Tx) error {
		return rs.inviteUserTx(tx, roomID, inviter, invitee)
	})
	if err != nil {
		return err
	}
	rs.s.Sync.Notify(roomID, 0)
	return nil
}

func (rs *RoomService) inviteUserTx(tx *sql.Tx, roomID, inviter, invitee string) error {
	if inviter != "" {
		m, err := rs.s.DB.GetMembershipTx(tx, roomID, inviter)
		if err != nil {
			return err
		}
		if m != models.MembershipJoin {
			return fmt.Errorf("inviter not in room")
		}
	}
	content, _ := json.Marshal(map[string]any{"membership": "invite"})
	_, _, err := rs.s.Timeline.createStateEventTx(tx, roomID, inviter, "m.room.member", invitee, content, nil)
	return err
}

// LeaveRoom makes a user leave a room.
func (rs *RoomService) LeaveRoom(roomID, userID string) error {
	m, err := rs.s.DB.GetMembership(roomID, userID)
	if err != nil {
		return err
	}
	if m != models.MembershipJoin && m != models.MembershipInvite {
		return fmt.Errorf("not in room")
	}
	content, _ := json.Marshal(map[string]any{"membership": "leave"})
	_, err = rs.s.Timeline.createStateEvent(nil, roomID, userID, "m.room.member", userID, content, nil)
	return err
}

// GetRoom returns a room.
func (rs *RoomService) GetRoom(roomID string) (*models.Room, error) {
	return rs.s.DB.GetRoom(roomID)
}

func (rs *RoomService) hasInviteState(roomID, userID string) bool {
	eventID, err := rs.s.DB.GetStateEvent(roomID, "m.room.member", userID)
	if err != nil || eventID == "" {
		return false
	}
	ev, err := rs.s.DB.GetEvent(eventID)
	if err != nil || ev == nil {
		return false
	}
	var content struct {
		Membership string `json:"membership"`
	}
	if err := json.Unmarshal(ev.Content, &content); err != nil {
		return false
	}
	return content.Membership == models.MembershipInvite
}

// ResolveAlias returns the room ID for an alias.
func (rs *RoomService) ResolveAlias(alias string) (string, error) {
	return rs.s.DB.GetRoomByAlias(alias)
}

// GetState returns current state events for a room.
func (rs *RoomService) GetState(roomID string) ([]*models.Event, error) {
	return rs.s.DB.GetAllStateEvents(roomID)
}

// GetStateEvent returns a specific state event.
func (rs *RoomService) GetStateEvent(roomID, eventType, stateKey string) (*models.Event, error) {
	eventID, err := rs.s.DB.GetStateEvent(roomID, eventType, stateKey)
	if err != nil {
		return nil, err
	}
	if eventID == "" {
		return nil, nil
	}
	return rs.s.DB.GetEvent(eventID)
}

// SetState sets a state event in a room.
func (rs *RoomService) SetState(roomID, sender, eventType, stateKey string, content json.RawMessage) (*models.Event, error) {
	if err := rs.ensureMember(roomID, sender); err != nil {
		return nil, err
	}
	if err := rs.checkPowerLevel(roomID, sender, eventType); err != nil {
		return nil, err
	}
	return rs.s.Timeline.createStateEvent(nil, roomID, sender, eventType, stateKey, content, nil)
}

// checkPowerLevel verifies the sender's power level allows sending the given
// state event type under the room's current m.room.power_levels.
func (rs *RoomService) checkPowerLevel(roomID, sender, eventType string) error {
	pl := rs.powerLevels(roomID)
	required := 50 // state_default per Matrix spec
	if v, ok := pl["state_default"]; ok {
		required = plInt(v, required)
	}
	if events, ok := pl["events"].(map[string]any); ok {
		if v, ok := events[eventType]; ok {
			required = plInt(v, required)
		}
	}
	senderLevel := 0 // users_default per Matrix spec
	if v, ok := pl["users_default"]; ok {
		senderLevel = plInt(v, senderLevel)
	}
	if users, ok := pl["users"].(map[string]any); ok {
		if v, ok := users[sender]; ok {
			senderLevel = plInt(v, senderLevel)
		}
	}
	if senderLevel < required {
		return fmt.Errorf("insufficient power level to set %s", eventType)
	}
	return nil
}

// powerLevels returns the room's current m.room.power_levels content, falling
// back to the default levels (creator at 100) when no such event exists.
func (rs *RoomService) powerLevels(roomID string) map[string]any {
	if ev, err := rs.GetStateEvent(roomID, "m.room.power_levels", ""); err == nil && ev != nil {
		var pl map[string]any
		if err := json.Unmarshal(ev.Content, &pl); err == nil && pl != nil {
			return pl
		}
	}
	creator := ""
	if room, err := rs.s.DB.GetRoom(roomID); err == nil && room != nil {
		creator = room.Creator
	}
	return defaultPowerLevels(creator)
}

// plInt reads a power level value that may arrive as int (defaults) or
// float64 (JSON-decoded event content).
func plInt(v any, fallback int) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return fallback
	}
}

// IsJoined reports whether the user is a joined member of the room.
func (rs *RoomService) IsJoined(roomID, userID string) (bool, error) {
	m, err := rs.s.DB.GetMembership(roomID, userID)
	if err != nil {
		return false, err
	}
	return m == models.MembershipJoin, nil
}

// ensureMember checks that a user is joined to a room.
func (rs *RoomService) ensureMember(roomID, userID string) error {
	m, err := rs.s.DB.GetMembership(roomID, userID)
	if err != nil {
		return err
	}
	if m != models.MembershipJoin {
		return fmt.Errorf("user not joined")
	}
	return nil
}

func defaultPowerLevels(creator string) map[string]any {
	return map[string]any{
		"ban":            50,
		"invite":         0,
		"kick":           50,
		"redact":         50,
		"events_default": 0,
		"state_default":  50,
		"users_default":  0,
		"users": map[string]any{
			creator: 100,
		},
		"events": map[string]any{
			"m.room.name":         50,
			"m.room.power_levels": 100,
		},
	}
}

func (rs *RoomService) userDisplayName(userID string) string {
	user, _ := rs.s.DB.GetUser(userID)
	if user != nil && user.DisplayName != nil && *user.DisplayName != "" {
		return *user.DisplayName
	}
	idx := strings.Index(userID, ":")
	if idx > 0 {
		return userID[1:idx]
	}
	return userID
}
