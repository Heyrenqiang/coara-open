package service

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"sync"

	"gomatrix/internal/models"
	"gomatrix/internal/utils"
)

// TimelineService creates and persists events.
type TimelineService struct {
	s *Services
	// writeMu serializes timeline appends (Conduit room insert lock — sync waits for idle).
	writeMu sync.Mutex
}

// NewTimelineService creates a timeline service.
func NewTimelineService(s *Services) *TimelineService {
	return &TimelineService{s: s}
}

// WaitTimelineIdle blocks until in-flight timeline writes finish (Conduit sync barrier).
func (ts *TimelineService) WaitTimelineIdle() {
	ts.writeMu.Lock()
	ts.writeMu.Unlock()
	ts.s.DB.WaitWriteIdle()
}

// SendMessage sends a non-state room message.
func (ts *TimelineService) SendMessage(roomID, sender, eventType string, content json.RawMessage) (*models.Event, error) {
	return ts.SendMessageWithTxn(roomID, sender, eventType, content, "")
}

// SendMessageWithTxn sends a message and deduplicates Matrix client transaction IDs.
//
// Lookup, event insert, and txn recording happen in one DB transaction under
// writeMu so concurrent retries with the same txnId cannot create two events.
func (ts *TimelineService) SendMessageWithTxn(roomID, sender, eventType string, content json.RawMessage, txnID string) (*models.Event, error) {
	if err := ts.s.Rooms.ensureMember(roomID, sender); err != nil {
		return nil, err
	}
	if txnID == "" {
		return ts.createEvent(nil, roomID, sender, eventType, nil, content, nil)
	}

	ts.writeMu.Lock()
	defer ts.writeMu.Unlock()

	var result *models.Event
	var stream int64
	var replayID string
	err := ts.s.DB.InTransaction(func(tx *sql.Tx) error {
		existing, err := ts.s.DB.LookupSentTransactionTx(tx, sender, roomID, txnID)
		if err != nil {
			return err
		}
		if existing != "" {
			replayID = existing
			return nil
		}
		ev, err := ts.buildEvent(tx, roomID, sender, eventType, nil, content, nil)
		if err != nil {
			return err
		}
		stream, err = ts.s.DB.SaveEvent(tx, ev)
		if err != nil {
			return err
		}
		if err := ts.s.DB.SaveSentTransaction(tx, sender, roomID, txnID, ev.EventID, ev.OriginServerTs); err != nil {
			return err
		}
		result = ev
		return nil
	})
	if err != nil {
		return nil, err
	}
	if replayID != "" {
		return ts.s.DB.GetEvent(replayID)
	}
	ts.s.Sync.Notify(result.RoomID, stream)
	return result, nil
}

// createStateEvent creates a state event in a transaction if provided.
func (ts *TimelineService) createStateEvent(tx *sql.Tx, roomID, sender, eventType, stateKey string, content json.RawMessage, redacts *string) (*models.Event, error) {
	if tx != nil {
		ev, _, err := ts.createStateEventTx(tx, roomID, sender, eventType, stateKey, content, redacts)
		return ev, err
	}
	ts.writeMu.Lock()
	defer ts.writeMu.Unlock()
	var ev *models.Event
	var stream int64
	err := ts.s.DB.InTransaction(func(inner *sql.Tx) error {
		var err error
		ev, stream, err = ts.createStateEventTx(inner, roomID, sender, eventType, stateKey, content, redacts)
		return err
	})
	if err != nil {
		return nil, err
	}
	ts.s.Sync.Notify(roomID, stream)
	return ev, nil
}

// createStateEventTx creates a state event inside an existing transaction.
func (ts *TimelineService) createStateEventTx(tx *sql.Tx, roomID, sender, eventType, stateKey string, content json.RawMessage, redacts *string) (*models.Event, int64, error) {
	ev, err := ts.buildEvent(tx, roomID, sender, eventType, &stateKey, content, redacts)
	if err != nil {
		return nil, 0, err
	}
	stream, err := ts.s.DB.SaveEvent(tx, ev)
	if err != nil {
		return nil, 0, err
	}
	if err := ts.s.DB.SetStateEvent(tx, roomID, eventType, stateKey, ev.EventID); err != nil {
		return nil, 0, err
	}
	// For membership events, update membership table
	if eventType == "m.room.member" {
		var membership struct {
			Membership string `json:"membership"`
		}
		_ = json.Unmarshal(content, &membership)
		displayName := ""
		avatarURL := ""
		if targetUser, _ := ts.s.DB.GetUserTx(tx, stateKey); targetUser != nil {
			if targetUser.DisplayName != nil {
				displayName = *targetUser.DisplayName
			}
			if targetUser.AvatarURL != nil {
				avatarURL = *targetUser.AvatarURL
			}
		}
		if err := ts.s.DB.SetMembershipTx(tx, roomID, stateKey, membership.Membership, displayName, avatarURL); err != nil {
			return nil, 0, err
		}
	}
	return ev, stream, nil
}

// createEvent creates a non-state event.
func (ts *TimelineService) createEvent(tx *sql.Tx, roomID, sender, eventType string, stateKey *string, content json.RawMessage, redacts *string) (*models.Event, error) {
	var ev *models.Event
	var stream int64
	exec := func(inner *sql.Tx) error {
		var err error
		ev, err = ts.buildEvent(inner, roomID, sender, eventType, stateKey, content, redacts)
		if err != nil {
			return err
		}
		stream, err = ts.s.DB.SaveEvent(inner, ev)
		return err
	}
	if tx != nil {
		if err := exec(tx); err != nil {
			return nil, err
		}
		return ev, nil
	}
	ts.writeMu.Lock()
	defer ts.writeMu.Unlock()
	if err := ts.s.DB.InTransaction(exec); err != nil {
		return nil, err
	}
	// Notify only after commit and while still holding writeMu — waking sync waits here.
	ts.s.Sync.Notify(ev.RoomID, stream)
	return ev, nil
}

func (ts *TimelineService) buildEvent(tx *sql.Tx, roomID, sender, eventType string, stateKey *string, content json.RawMessage, redacts *string) (*models.Event, error) {
	depth, err := ts.nextDepth(tx, roomID)
	if err != nil {
		return nil, err
	}
	prevEvents, err := ts.prevEvents(tx, roomID)
	if err != nil {
		return nil, err
	}
	ev := &models.Event{
		EventID:        utils.GenerateEventID(ts.s.Config.ServerName),
		RoomID:         roomID,
		Sender:         sender,
		Type:           eventType,
		StateKey:       stateKey,
		Content:        content,
		PrevEvents:     prevEvents,
		OriginServerTs: utils.NowMillis(),
		Depth:          depth,
		Unsigned:       json.RawMessage("{}"),
		Redacts:        redacts,
	}
	return ev, nil
}

func (ts *TimelineService) nextDepth(tx *sql.Tx, roomID string) (int, error) {
	var maxDepth int
	err := tx.QueryRow(`SELECT COALESCE(MAX(depth), 0) FROM events WHERE room_id = ?`, roomID).Scan(&maxDepth)
	if err != nil {
		return 0, err
	}
	return maxDepth + 1, nil
}

func (ts *TimelineService) prevEvents(tx *sql.Tx, roomID string) ([]string, error) {
	var eventID string
	err := tx.QueryRow(
		`SELECT event_id FROM timeline WHERE room_id = ? ORDER BY stream_ordering DESC LIMIT 1`,
		roomID,
	).Scan(&eventID)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return []string{eventID}, nil
}

// GetMessages returns messages for /rooms/{id}/messages.
func (ts *TimelineService) GetMessages(roomID string, from, to int64, limit int, backward bool) ([]*models.Event, string, string, error) {
	if from == 0 && backward {
		max, err := ts.s.DB.GetRoomMaxStreamOrdering(roomID)
		if err != nil {
			return nil, "", "", err
		}
		from = max + 1
	}
	if to == 0 {
		if backward {
			to = 0
		} else {
			to = from + 1000
		}
	}
	entries, err := ts.s.DB.GetTimelineForRoom(roomID, from, to, limit, backward)
	if err != nil {
		return nil, "", "", err
	}
	var events []*models.Event
	for _, e := range entries {
		events = append(events, e.Event)
	}
	var start, end string
	if len(entries) > 0 {
		if backward {
			start = fmt.Sprintf("t%d", entries[0].StreamOrdering)
			end = fmt.Sprintf("t%d", entries[len(entries)-1].StreamOrdering)
		} else {
			start = fmt.Sprintf("t%d", entries[0].StreamOrdering)
			end = fmt.Sprintf("t%d", entries[len(entries)-1].StreamOrdering)
		}
	}
	return events, start, end, nil
}
