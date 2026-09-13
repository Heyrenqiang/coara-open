package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"gomatrix/internal/models"
)

const syncTimelineLimit = 1000

type syncWaitRegistry struct {
	mu      sync.Mutex
	waiters map[string][]chan struct{}
}

func newSyncWaitRegistry() *syncWaitRegistry {
	return &syncWaitRegistry{waiters: make(map[string][]chan struct{})}
}

func syncWaitKey(userID, deviceID string) string {
	if deviceID == "" {
		deviceID = "_"
	}
	return userID + "\x00" + deviceID
}

func (r *syncWaitRegistry) register(key string) chan struct{} {
	ch := make(chan struct{}, 1)
	r.mu.Lock()
	r.waiters[key] = append(r.waiters[key], ch)
	r.mu.Unlock()
	return ch
}

func (r *syncWaitRegistry) unregister(key string, ch chan struct{}) {
	r.mu.Lock()
	list := r.waiters[key]
	for i, candidate := range list {
		if candidate == ch {
			r.waiters[key] = append(list[:i], list[i+1:]...)
			break
		}
	}
	if len(r.waiters[key]) == 0 {
		delete(r.waiters, key)
	}
	r.mu.Unlock()
}

func (r *syncWaitRegistry) notifyUsers(userIDs []string) {
	if len(userIDs) == 0 {
		return
	}
	allowed := make(map[string]struct{}, len(userIDs))
	for _, uid := range userIDs {
		allowed[uid] = struct{}{}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for key, list := range r.waiters {
		userID, _, _ := strings.Cut(key, "\x00")
		if _, ok := allowed[userID]; !ok {
			continue
		}
		for _, ch := range list {
			select {
			case ch <- struct{}{}:
			default:
			}
		}
	}
}

// SyncService handles /sync long-polling and notifications.
type SyncService struct {
	s       *Services
	waiters *syncWaitRegistry
}

// NewSyncService creates a sync service.
func NewSyncService(s *Services) *SyncService {
	return &SyncService{
		s:       s,
		waiters: newSyncWaitRegistry(),
	}
}

// Notify wakes sync waiters for users joined to roomID.
func (ss *SyncService) Notify(roomID string, stream int64) {
	userIDs, err := ss.s.DB.GetSyncNotifyUserIDsForRoom(roomID)
	if err != nil {
		slog.Warn("sync notify lookup failed", "room_id", roomID, "error", err)
		return
	}
	ss.waiters.notifyUsers(userIDs)
	_ = stream
}

// NotifyRoomEphemeral wakes sync waiters after m.typing changes (no timeline row).
func (ss *SyncService) NotifyRoomEphemeral(roomID string) {
	ss.Notify(roomID, 0)
}

// Sync builds a sync response.
func (ss *SyncService) Sync(ctx context.Context, userID, deviceID, since string, timeout time.Duration) (*models.SyncResponse, error) {
	if since == "" {
		return ss.initialSync(userID)
	}

	fromStream, err := parseSyncToken(since)
	if err != nil {
		return nil, ErrInvalidSyncToken
	}

	hasUpdates, err := ss.s.DB.HasStreamUpdatesSince(fromStream)
	if err != nil {
		return nil, err
	}

	if !hasUpdates && timeout > 0 {
		key := syncWaitKey(userID, deviceID)
		ch := ss.waiters.register(key)
		// Re-check AFTER registering: an event landing between the first check
		// and the register would otherwise be a missed wakeup — the waiter
		// would sleep the full timeout despite pending events.
		hasUpdates, err = ss.s.DB.HasStreamUpdatesSince(fromStream)
		if err != nil {
			ss.waiters.unregister(key, ch)
			return nil, err
		}
		if hasUpdates {
			ss.waiters.unregister(key, ch)
			return ss.incrementalSync(userID, fromStream)
		}
		defer ss.waiters.unregister(key, ch)

		timer := time.NewTimer(timeout)
		defer timer.Stop()

		select {
		case <-ch:
		case <-timer.C:
			hasUpdates, err = ss.s.DB.HasStreamUpdatesSince(fromStream)
			if err != nil {
				return nil, err
			}
			if !hasUpdates {
				return ss.emptyIncrementalSync(since), nil
			}
		case <-ctx.Done():
			return ss.emptyIncrementalSync(since), nil
		}
	}

	return ss.incrementalSync(userID, fromStream)
}

func (ss *SyncService) emptyIncrementalSync(since string) *models.SyncResponse {
	resp := &models.SyncResponse{
		NextBatch: since,
	}
	resp.Rooms.Join = make(map[string]*models.JoinedRoom)
	resp.Rooms.Invite = make(map[string]*models.InvitedRoom)
	resp.Rooms.Leave = make(map[string]*models.LeftRoom)
	normalizeSyncResponse(resp)
	return resp
}

func (ss *SyncService) initialSync(userID string) (*models.SyncResponse, error) {
	resp := &models.SyncResponse{}
	resp.Rooms.Join = make(map[string]*models.JoinedRoom)
	resp.Rooms.Invite = make(map[string]*models.InvitedRoom)
	resp.Rooms.Leave = make(map[string]*models.LeftRoom)

	maxStream, err := ss.s.DB.GetMaxStreamOrdering()
	if err != nil {
		return nil, err
	}
	resp.NextBatch = fmt.Sprintf("s%d", maxStream)

	joined, err := ss.s.DB.GetJoinedRoomsForUser(userID)
	if err != nil {
		return nil, err
	}
	for _, roomID := range joined {
		jr, _, err := ss.buildJoinedRoom(userID, roomID, 0, 0, true)
		if err != nil {
			return nil, err
		}
		resp.Rooms.Join[roomID] = jr
	}

	invited, err := ss.s.DB.GetInvitedRoomsForUser(userID)
	if err != nil {
		return nil, err
	}
	for _, roomID := range invited {
		ir, err := ss.buildInvitedRoom(roomID, userID)
		if err != nil {
			return nil, err
		}
		resp.Rooms.Invite[roomID] = ir
	}

	left, err := ss.s.DB.GetLeftRoomsForUser(userID)
	if err != nil {
		return nil, err
	}
	for _, roomID := range left {
		lr := &models.LeftRoom{}
		resp.Rooms.Leave[roomID] = lr
	}

	account, err := ss.s.DB.GetAllAccountData(userID)
	if err != nil {
		slog.Warn("sync account_data read failed", "user_id", userID, "error", err)
	} else {
		for _, a := range account {
			if a.RoomID == nil {
				resp.AccountData.Events = append(resp.AccountData.Events, models.AccountDataEvent{
					Type:    a.Type,
					Content: a.Content,
				})
			}
		}
	}

	devices, err := ss.s.DB.GetDevicesForUser(userID)
	if err != nil {
		slog.Warn("sync devices read failed", "user_id", userID, "error", err)
	} else {
		for _, d := range devices {
			msgs, msgErr := ss.s.DB.GetToDeviceMessages(userID, d.DeviceID, true)
			if msgErr != nil {
				slog.Warn("sync to_device read failed", "user_id", userID, "device_id", d.DeviceID, "error", msgErr)
				continue
			}
			for _, m := range msgs {
				resp.ToDevice.Events = append(resp.ToDevice.Events, models.ToDeviceEvent{
					Type:      m.Type,
					Content:   m.Content,
					MessageID: m.MessageID,
				})
			}
		}
	}

	normalizeSyncResponse(resp)
	return resp, nil
}

func (ss *SyncService) incrementalSync(userID string, fromStream int64) (*models.SyncResponse, error) {
	// Conduit waits for room insert locks before reading timeline — same barrier here.
	ss.s.Timeline.WaitTimelineIdle()

	resp := &models.SyncResponse{}
	resp.Rooms.Join = make(map[string]*models.JoinedRoom)
	resp.Rooms.Invite = make(map[string]*models.InvitedRoom)
	resp.Rooms.Leave = make(map[string]*models.LeftRoom)

	toStream, err := ss.s.DB.GetMaxStreamOrdering()
	if err != nil {
		return nil, err
	}

	deliveredUpTo := fromStream

	dirtyRooms, err := ss.s.DB.GetDirtyJoinedRoomIDs(userID, fromStream)
	if err != nil {
		return nil, err
	}
	for _, roomID := range dirtyRooms {
		membership, _ := ss.s.DB.GetMembership(roomID, userID)
		switch membership {
		case models.MembershipJoin:
			jr, roomDelivered, buildErr := ss.buildJoinedRoom(userID, roomID, fromStream, toStream, false)
			if buildErr != nil {
				return nil, buildErr
			}
			if roomDelivered > deliveredUpTo {
				deliveredUpTo = roomDelivered
			}
			if len(jr.Timeline.Events) > 0 || len(jr.State.Events) > 0 || len(jr.Ephemeral.Events) > 0 {
				resp.Rooms.Join[roomID] = jr
			}
		case models.MembershipInvite:
			ir, buildErr := ss.buildInvitedRoom(roomID, userID)
			if buildErr != nil {
				return nil, buildErr
			}
			resp.Rooms.Invite[roomID] = ir
			// Personal-server bots must see timeline while still invited (matrix-nio rooms.join).
			jr, roomDelivered, buildErr := ss.buildJoinedRoom(userID, roomID, fromStream, toStream, false)
			if buildErr != nil {
				return nil, buildErr
			}
			if roomDelivered > deliveredUpTo {
				deliveredUpTo = roomDelivered
			}
			if len(jr.Timeline.Events) > 0 || len(jr.Ephemeral.Events) > 0 {
				resp.Rooms.Join[roomID] = jr
			}
		}
	}

	if err := ss.sweepUndeliveredJoinRooms(userID, fromStream, toStream, resp, &deliveredUpTo); err != nil {
		return nil, err
	}

	// Always surface pending invites until the client joins (avoid missed-invite deadlock).
	pendingInvites, err := ss.s.DB.GetInvitedRoomsForUser(userID)
	if err != nil {
		return nil, err
	}
	for _, roomID := range pendingInvites {
		if _, exists := resp.Rooms.Invite[roomID]; exists {
			continue
		}
		ir, buildErr := ss.buildInvitedRoom(roomID, userID)
		if buildErr != nil {
			return nil, buildErr
		}
		resp.Rooms.Invite[roomID] = ir
	}

	ss.attachEphemeralOnlyJoinedRooms(userID, resp)

	resp.NextBatch = ss.resolveNextBatch(userID, fromStream, deliveredUpTo, toStream)

	normalizeSyncResponse(resp)
	return resp, nil
}

func (ss *SyncService) resolveNextBatch(userID string, fromStream, deliveredUpTo, toStream int64) string {
	if deliveredUpTo > fromStream {
		return fmt.Sprintf("s%d", deliveredUpTo)
	}
	pending, err := ss.s.DB.HasJoinStreamUpdatesForUser(userID, fromStream)
	if err != nil {
		slog.Warn("sync pending check failed", "user_id", userID, "error", err)
		// Fail closed: never skip events when we cannot verify delivery.
		return fmt.Sprintf("s%d", fromStream)
	}
	if pending {
		slog.Debug(
			"sync withheld next_batch — joined-room timeline not yet delivered",
			"user_id", userID,
			"since", fromStream,
			"to", toStream,
		)
		return fmt.Sprintf("s%d", fromStream)
	}
	if toStream > fromStream {
		return fmt.Sprintf("s%d", toStream)
	}
	return fmt.Sprintf("s%d", fromStream)
}

// sweepUndeliveredJoinRooms catches joined rooms missed by the dirty-room query.
func (ss *SyncService) sweepUndeliveredJoinRooms(
	userID string,
	fromStream, toStream int64,
	resp *models.SyncResponse,
	deliveredUpTo *int64,
) error {
	joined, err := ss.s.DB.GetJoinedRoomsForUser(userID)
	if err != nil {
		return err
	}
	for _, roomID := range joined {
		if _, exists := resp.Rooms.Join[roomID]; exists {
			continue
		}
		has, err := ss.s.DB.HasRoomStreamUpdatesSince(roomID, fromStream)
		if err != nil || !has {
			continue
		}
		jr, roomDelivered, buildErr := ss.buildJoinedRoom(userID, roomID, fromStream, toStream, false)
		if buildErr != nil {
			return buildErr
		}
		if roomDelivered > *deliveredUpTo {
			*deliveredUpTo = roomDelivered
		}
		if len(jr.Timeline.Events) > 0 || len(jr.State.Events) > 0 || len(jr.Ephemeral.Events) > 0 {
			resp.Rooms.Join[roomID] = jr
		}
	}
	return nil
}

func (ss *SyncService) buildJoinedRoom(userID, roomID string, since, toStream int64, initial bool) (*models.JoinedRoom, int64, error) {
	jr := &models.JoinedRoom{}
	normalizeJoinedRoom(jr)
	deliveredUpTo := since

	maxStream, err := ss.s.DB.GetMaxStreamOrdering()
	if err != nil {
		return nil, since, err
	}

	if initial {
		// Initial /sync (no since): return room state + position tip only.
		// Never dump historic timeline here — matrix-nio bots fire a callback per
		// timeline event, so a full history dump reprocesses every past message
		// whenever a client syncs without since (lost token, first login, or
		// post-homeserver-restart reconnect that cleared next_batch).
		// History belongs on GET /messages (Android hydrateRoomTimeline).
		stateEvents, err := ss.s.DB.GetAllStateEvents(roomID)
		if err != nil {
			return nil, since, err
		}
		jr.State.Events = nonNullEvents(stateEvents)
		jr.Timeline.Events = []*models.Event{}
		deliveredUpTo = maxStream
	} else {
		upper := toStream
		if upper <= 0 || upper > maxStream {
			upper = maxStream
		}
		entries, err := ss.s.DB.GetTimelineForRoom(roomID, since, upper, syncTimelineLimit, false)
		if err != nil {
			return nil, since, err
		}
		events := make([]*models.Event, 0, len(entries))
		for _, entry := range entries {
			events = append(events, entry.Event)
			if entry.StreamOrdering > deliveredUpTo {
				deliveredUpTo = entry.StreamOrdering
			}
		}
		jr.Timeline.Events = nonNullEvents(events)
		if len(entries) >= syncTimelineLimit {
			jr.Timeline.Limited = true
			if entries[0].StreamOrdering > 0 {
				jr.Timeline.PrevBatch = fmt.Sprintf("s%d", entries[0].StreamOrdering-1)
			}
		}
	}

	typingEvents := ss.s.Typing.EphemeralEvents(roomID, userID)
	if len(typingEvents) > 0 {
		jr.Ephemeral.Events = typingEvents
	}

	joinedCount, _ := ss.s.DB.CountRoomMembers(roomID, models.MembershipJoin)
	invitedCount, _ := ss.s.DB.CountRoomMembers(roomID, models.MembershipInvite)
	if joinedCount > 0 {
		jc := joinedCount
		jr.Summary.JoinedMemberCount = &jc
	}
	if invitedCount > 0 {
		ic := invitedCount
		jr.Summary.InvitedMemberCount = &ic
	}

	return jr, deliveredUpTo, nil
}

// attachEphemeralOnlyJoinedRooms adds m.typing when a room had no timeline delta this sync.
func (ss *SyncService) attachEphemeralOnlyJoinedRooms(userID string, resp *models.SyncResponse) {
	joined, err := ss.s.DB.GetJoinedRoomsForUser(userID)
	if err != nil {
		slog.Warn("sync ephemeral attach failed", "user_id", userID, "error", err)
		return
	}
	for _, roomID := range joined {
		if _, exists := resp.Rooms.Join[roomID]; exists {
			continue
		}
		typingEvents := ss.s.Typing.EphemeralEvents(roomID, userID)
		if len(typingEvents) == 0 {
			continue
		}
		jr := &models.JoinedRoom{}
		normalizeJoinedRoom(jr)
		jr.Ephemeral.Events = typingEvents
		resp.Rooms.Join[roomID] = jr
	}
}

// normalizeJoinedRoom ensures matrix-nio-compatible empty arrays (never JSON null).
func normalizeJoinedRoom(jr *models.JoinedRoom) {
	if jr == nil {
		return
	}
	jr.Timeline.Events = nonNullEvents(jr.Timeline.Events)
	jr.State.Events = nonNullEvents(jr.State.Events)
	if jr.Ephemeral.Events == nil {
		jr.Ephemeral.Events = []json.RawMessage{}
	}
	if jr.AccountData.Events == nil {
		jr.AccountData.Events = []models.AccountDataEvent{}
	}
}

func nonNullEvents(events []*models.Event) []*models.Event {
	if events == nil {
		return []*models.Event{}
	}
	return events
}

func normalizeSyncResponse(resp *models.SyncResponse) {
	if resp == nil {
		return
	}
	if resp.AccountData.Events == nil {
		resp.AccountData.Events = []models.AccountDataEvent{}
	}
	if resp.ToDevice.Events == nil {
		resp.ToDevice.Events = []models.ToDeviceEvent{}
	}
	if resp.Presence.Events == nil {
		resp.Presence.Events = []json.RawMessage{}
	}
	if resp.Rooms.Join == nil {
		resp.Rooms.Join = make(map[string]*models.JoinedRoom)
	}
	if resp.Rooms.Invite == nil {
		resp.Rooms.Invite = make(map[string]*models.InvitedRoom)
	}
	if resp.Rooms.Leave == nil {
		resp.Rooms.Leave = make(map[string]*models.LeftRoom)
	}
}

func (ss *SyncService) buildInvitedRoom(roomID, userID string) (*models.InvitedRoom, error) {
	ir := &models.InvitedRoom{}
	inviteEventID, err := ss.s.DB.GetStateEvent(roomID, "m.room.member", userID)
	if err != nil {
		return nil, err
	}
	if inviteEventID != "" {
		ev, err := ss.s.DB.GetEvent(inviteEventID)
		if err != nil {
			return nil, err
		}
		if ev != nil {
			ir.InviteState.Events = append(ir.InviteState.Events, ev)
		}
	}
	if ir.InviteState.Events == nil {
		ir.InviteState.Events = []*models.Event{}
	}
	return ir, nil
}

func parseSyncToken(token string) (int64, error) {
	if len(token) < 2 || token[0] != 's' {
		return 0, fmt.Errorf("invalid sync token")
	}
	n, err := strconv.ParseInt(token[1:], 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid sync token")
	}
	return n, nil
}
