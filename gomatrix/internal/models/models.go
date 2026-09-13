package models

import (
	"database/sql"
	"encoding/json"
	"time"
)

// User represents a Matrix account.
type User struct {
	UserID       string    `json:"user_id"`
	PasswordHash string    `json:"-"`
	DisplayName  *string   `json:"displayname,omitempty"`
	AvatarURL    *string   `json:"avatar_url,omitempty"`
	Admin        bool      `json:"admin,omitempty"`
	CreatedAt    time.Time `json:"created_at"`
}

// Device represents a logged-in client device.
type Device struct {
	DeviceID    string       `json:"device_id"`
	UserID      string       `json:"user_id"`
	DisplayName string       `json:"display_name,omitempty"`
	LastSeenIP  string       `json:"last_seen_ip,omitempty"`
	LastSeenAt  sql.NullTime `json:"last_seen_ts,omitempty"`
}

// Room represents a Matrix room.
type Room struct {
	RoomID         string    `json:"room_id"`
	Version        string    `json:"room_version"`
	Creator        string    `json:"creator"`
	Name           *string   `json:"name,omitempty"`
	Topic          *string   `json:"topic,omitempty"`
	CanonicalAlias *string   `json:"canonical_alias,omitempty"`
	JoinRules      string    `json:"join_rules,omitempty"`
	Encrypted      bool      `json:"encrypted,omitempty"`
	CreatedAt      time.Time `json:"created_at"`
}

// Membership values.
const (
	MembershipJoin   = "join"
	MembershipInvite = "invite"
	MembershipLeave  = "leave"
	MembershipBan    = "ban"
	MembershipKnock  = "knock"
)

// RoomMember represents membership state in a room.
type RoomMember struct {
	RoomID      string `json:"room_id"`
	UserID      string `json:"user_id"`
	Membership  string `json:"membership"`
	DisplayName string `json:"displayname,omitempty"`
	AvatarURL   string `json:"avatar_url,omitempty"`
}

// Event represents a Matrix PDU/event.
type Event struct {
	EventID        string          `json:"event_id"`
	RoomID         string          `json:"room_id"`
	Sender         string          `json:"sender"`
	Type           string          `json:"type"`
	StateKey       *string         `json:"state_key,omitempty"`
	Content        json.RawMessage `json:"content"`
	PrevEvents     []string        `json:"prev_events,omitempty"`
	OriginServerTs int64           `json:"origin_server_ts"`
	Depth          int             `json:"depth"`
	Unsigned       json.RawMessage `json:"unsigned,omitempty"`
	Redacts        *string         `json:"redacts,omitempty"`
}

// IsState returns true if the event is a state event.
func (e *Event) IsState() bool {
	return e.StateKey != nil
}

// TimelineEntry joins a room with an event at a stream position.
type TimelineEntry struct {
	StreamOrdering int64  `json:"stream_ordering"`
	RoomID         string `json:"room_id"`
	EventID        string `json:"event_id"`
}

// AccountData represents per-user or per-room account data.
type AccountData struct {
	UserID  string          `json:"user_id"`
	RoomID  *string         `json:"room_id,omitempty"`
	Type    string          `json:"type"`
	Content json.RawMessage `json:"content"`
}

// ToDeviceMessage represents a message queued for a device.
type ToDeviceMessage struct {
	ID        int64           `json:"-"`
	UserID    string          `json:"-"`
	DeviceID  string          `json:"-"`
	Type      string          `json:"type"`
	Content   json.RawMessage `json:"content"`
	MessageID string          `json:"message_id"`
}

// Media represents an uploaded file.
type Media struct {
	MediaID     string    `json:"media_id"`
	SHA256      string    `json:"sha256"`
	ContentType string    `json:"content_type"`
	Size        int64     `json:"size"`
	FilePath    string    `json:"file_path"`
	CreatedAt   time.Time `json:"created_at"`
}

// SyncResponse mirrors the Matrix /sync response.
type SyncResponse struct {
	NextBatch string `json:"next_batch"`
	Rooms     struct {
		Join   map[string]*JoinedRoom  `json:"join"`
		Invite map[string]*InvitedRoom `json:"invite"`
		Leave  map[string]*LeftRoom    `json:"leave"`
	} `json:"rooms"`
	AccountData struct {
		Events []AccountDataEvent `json:"events"`
	} `json:"account_data"`
	ToDevice struct {
		Events []ToDeviceEvent `json:"events"`
	} `json:"to_device"`
	Presence struct {
		Events []json.RawMessage `json:"events"`
	} `json:"presence"`
}

// AccountDataEvent is a single account_data event.
type AccountDataEvent struct {
	Type    string          `json:"type"`
	Content json.RawMessage `json:"content"`
}

// ToDeviceEvent is a single to_device event.
type ToDeviceEvent struct {
	Sender    string          `json:"sender"`
	Type      string          `json:"type"`
	Content   json.RawMessage `json:"content"`
	MessageID string          `json:"message_id,omitempty"`
}

// JoinedRoom is the sync data for a joined room.
type JoinedRoom struct {
	Timeline struct {
		Events    []*Event `json:"events"`
		Limited   bool     `json:"limited,omitempty"`
		PrevBatch string   `json:"prev_batch,omitempty"`
	} `json:"timeline"`
	State struct {
		Events []*Event `json:"events"`
	} `json:"state"`
	Ephemeral struct {
		Events []json.RawMessage `json:"events"`
	} `json:"ephemeral"`
	AccountData struct {
		Events []AccountDataEvent `json:"events"`
	} `json:"account_data"`
	UnreadNotifications struct {
		HighlightCount    int `json:"highlight_count,omitempty"`
		NotificationCount int `json:"notification_count,omitempty"`
	} `json:"unread_notifications,omitempty"`
	Summary struct {
		Heroes             []string `json:"m.heroes,omitempty"`
		JoinedMemberCount  *int     `json:"m.joined_member_count,omitempty"`
		InvitedMemberCount *int     `json:"m.invited_member_count,omitempty"`
	} `json:"summary,omitempty"`
}

// InvitedRoom is the sync data for an invited room.
type InvitedRoom struct {
	InviteState struct {
		Events []*Event `json:"events"`
	} `json:"invite_state"`
}

// LeftRoom is the sync data for a left room.
type LeftRoom struct {
	Timeline struct {
		Events    []*Event `json:"events"`
		Limited   bool     `json:"limited,omitempty"`
		PrevBatch string   `json:"prev_batch,omitempty"`
	} `json:"timeline"`
	State struct {
		Events []*Event `json:"events"`
	} `json:"state"`
	AccountData struct {
		Events []AccountDataEvent `json:"events"`
	} `json:"account_data"`
}
