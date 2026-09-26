package service

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"golang.org/x/crypto/bcrypt"
	"gomatrix/internal/config"
	"gomatrix/internal/models"
	"gomatrix/internal/utils"
)

// AgentInfo describes a registered agent for the /api/agents endpoint.
type AgentInfo struct {
	Name        string `json:"name"`
	UserID      string `json:"user_id"`
	DisplayName string `json:"display_name"`
	Default     bool   `json:"default"`
	Connected   bool   `json:"connected"`
}

// EnsureAgents registers all configured agent accounts on startup.
// Existing agent passwords are reset from configuration on startup.
// After registration, a shared room is created (if missing) and all agents are invited.
func EnsureAgents(s *Services) error {
	if len(s.Config.Agents) == 0 {
		slog.Info("no agents configured, skipping auto-registration")
		return nil
	}

	var agentUserIDs []string
	for _, ac := range s.Config.Agents {
		userID, err := ensureAgentAccount(s, ac)
		if err != nil {
			slog.Warn("agent auto-registration failed", "name", ac.Name, "error", err)
			continue
		}
		agentUserIDs = append(agentUserIDs, userID)
		slog.Info("agent ready", "name", ac.Name, "user_id", userID, "default", ac.Default)
	}

	if len(agentUserIDs) < 2 {
		slog.Info("fewer than 2 agents registered, skipping shared room creation")
		return nil
	}

	// Create or find the shared agent room.
	roomID, err := ensureSharedRoom(s, agentUserIDs)
	if err != nil {
		slog.Warn("shared room creation failed", "error", err)
	} else {
		slog.Info("shared agent room ready", "room_id", roomID)
	}

	return nil
}

// ensureAgentAccount registers a single agent account if it doesn't exist.
func ensureAgentAccount(s *Services, ac config.AgentConfig) (string, error) {
	localpart := strings.ToLower(strings.TrimSpace(ac.Name))
	if localpart == "" {
		return "", fmt.Errorf("agent name is empty")
	}
	if strings.TrimSpace(ac.Password) == "" {
		return "", fmt.Errorf("agent %q password is empty", localpart)
	}
	userID := utils.UserID(localpart, s.Config.ServerName)

	exists, err := s.DB.UserExists(userID)
	if err != nil {
		return "", fmt.Errorf("check user exists: %w", err)
	}
	if exists {
		hash, hashErr := bcrypt.GenerateFromPassword([]byte(ac.Password), bcrypt.DefaultCost)
		if hashErr != nil {
			return "", fmt.Errorf("hash password: %w", hashErr)
		}
		if err := s.DB.SetPassword(userID, string(hash)); err != nil {
			return "", fmt.Errorf("reset agent password: %w", err)
		}
		// Update display name if configured and different.
		if ac.DisplayName != "" {
			user, _ := s.DB.GetUser(userID)
			if user != nil {
				if user.DisplayName == nil || *user.DisplayName != ac.DisplayName {
					_ = s.DB.UpdateProfile(userID, ac.DisplayName, "")
				}
			}
		}
		return userID, nil
	}

	// Hash the password and create the agent account.
	hash, err := bcrypt.GenerateFromPassword([]byte(ac.Password), bcrypt.DefaultCost)
	if err != nil {
		return "", fmt.Errorf("hash password: %w", err)
	}
	if err := s.DB.CreateUser(userID, string(hash), false); err != nil {
		return "", fmt.Errorf("create agent user: %w", err)
	}

	// Set display name if configured.
	if ac.DisplayName != "" {
		_ = s.DB.UpdateProfile(userID, ac.DisplayName, "")
	}

	slog.Info("agent registered", "name", localpart, "user_id", userID)
	return userID, nil
}

// ensureSharedRoom creates a shared room for all agents if it doesn't exist yet.
// The first agent in the list is the creator; all others are invited.
func ensureSharedRoom(s *Services, agentUserIDs []string) (string, error) {
	if len(agentUserIDs) < 2 {
		return "", fmt.Errorf("need at least 2 agents for shared room")
	}

	// Check if a shared room already exists by looking for a room with
	// canonical alias "#agents:<server_name>".
	alias := "#agents:" + s.Config.ServerName
	existingRoomID, err := s.DB.GetRoomByAlias(alias)
	if err == nil && existingRoomID != "" {
		// Room already exists; ensure all agents are members.
		for _, uid := range agentUserIDs {
			m, _ := s.DB.GetMembership(existingRoomID, uid)
			switch m {
			case models.MembershipJoin:
				continue
			case models.MembershipInvite:
				content, _ := json.Marshal(map[string]any{"membership": "join"})
				_, _ = s.Timeline.createStateEvent(nil, existingRoomID, uid, "m.room.member", uid, content, nil)
			default:
				_ = s.Rooms.InviteUser(existingRoomID, agentUserIDs[0], uid)
			}
		}
		return existingRoomID, nil
	}

	// Create the shared room.
	creator := agentUserIDs[0]
	invite := agentUserIDs[1:]
	roomName := "Agents Shared Room"

	req := &CreateRoomRequest{
		Preset:        "trusted_private_chat",
		Name:          roomName,
		RoomAliasName: "agents",
		Invite:        invite,
	}

	room, err := s.Rooms.CreateRoom(creator, req)
	if err != nil {
		return "", fmt.Errorf("create shared room: %w", err)
	}

	// Auto-join invited agents (bots will also join via /sync, but we force-join here).
	for _, uid := range invite {
		content, _ := json.Marshal(map[string]any{"membership": "join"})
		_, _ = s.Timeline.createStateEvent(nil, room.RoomID, uid, "m.room.member", uid, content, nil)
	}

	return room.RoomID, nil
}

// ListAgents returns configured agents with live connection status.
func ListAgents(s *Services) []AgentInfo {
	const maxIdleMs int64 = 3 * 60 * 1000
	result := make([]AgentInfo, 0, len(s.Config.Agents))
	for _, ac := range s.Config.Agents {
		localpart := strings.ToLower(strings.TrimSpace(ac.Name))
		if localpart == "" {
			continue
		}
		userID := utils.UserID(localpart, s.Config.ServerName)
		active, err := s.DB.UserRecentlyActive(userID, maxIdleMs)
		connected := err == nil && active
		result = append(result, AgentInfo{
			Name:        localpart,
			UserID:      userID,
			DisplayName: ac.DisplayName,
			Default:     ac.Default,
			Connected:   connected,
		})
	}
	return result
}

// AnyPhoneClientConnected reports whether any non-agent Matrix user synced recently.
// Used by WebUI to hide the「连接手机」nav entry while a phone app is online.
func AnyPhoneClientConnected(s *Services) bool {
	const maxIdleMs int64 = 5 * 60 * 1000
	agentIDs := make(map[string]struct{}, len(s.Config.Agents))
	for _, ac := range s.Config.Agents {
		localpart := strings.ToLower(strings.TrimSpace(ac.Name))
		if localpart == "" {
			continue
		}
		agentIDs[utils.UserID(localpart, s.Config.ServerName)] = struct{}{}
	}
	uids, err := s.DB.RecentlyActiveUserIDs(maxIdleMs)
	if err != nil {
		return false
	}
	for _, uid := range uids {
		if _, isAgent := agentIDs[uid]; !isAgent {
			return true
		}
	}
	return false
}
