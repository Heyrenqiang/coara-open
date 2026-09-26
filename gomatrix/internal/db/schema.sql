-- Users
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    avatar_url TEXT,
    admin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

-- Devices
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    display_name TEXT,
    last_seen_ip TEXT,
    last_seen_at INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

-- Access tokens
CREATE TABLE IF NOT EXISTS access_tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    device_id TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON access_tokens(user_id);

-- Rooms
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    creator TEXT NOT NULL,
    name TEXT,
    topic TEXT,
    canonical_alias TEXT,
    encrypted INTEGER NOT NULL DEFAULT 0,
    join_rules TEXT NOT NULL DEFAULT 'invite',
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_alias ON rooms(canonical_alias) WHERE canonical_alias IS NOT NULL;

-- Room aliases
CREATE TABLE IF NOT EXISTS room_aliases (
    alias TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    creator TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_aliases_room ON room_aliases(room_id);

-- Room members
CREATE TABLE IF NOT EXISTS room_members (
    room_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    membership TEXT NOT NULL,
    display_name TEXT,
    avatar_url TEXT,
    PRIMARY KEY (room_id, user_id),
    FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_membership_user ON room_members(user_id);

-- Events (PDUs)
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state_key TEXT,
    content TEXT NOT NULL,
    prev_events TEXT,
    origin_server_ts INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    unsigned TEXT,
    redacts TEXT,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_room ON events(room_id);
CREATE INDEX IF NOT EXISTS idx_events_sender ON events(sender);

-- Timeline ordering
CREATE TABLE IF NOT EXISTS timeline (
    stream_ordering INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_timeline_room ON timeline(room_id, stream_ordering);

-- Current state per room
CREATE TABLE IF NOT EXISTS state_events (
    room_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (room_id, event_type, state_key),
    FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

-- Account data
CREATE TABLE IF NOT EXISTS account_data (
    user_id TEXT NOT NULL,
    room_id TEXT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (user_id, room_id, type)
);

-- To-device messages
CREATE TABLE IF NOT EXISTS to_device (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    message_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todevice_user ON to_device(user_id, device_id);

-- Send transaction dedup (Matrix PUT .../send/.../txnId)
CREATE TABLE IF NOT EXISTS sent_transactions (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    txn_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, room_id, txn_id)
);

-- Media
CREATE TABLE IF NOT EXISTS media (
    media_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media(sha256);

-- Federation server keys cache (optional)
CREATE TABLE IF NOT EXISTS server_keys (
    server_name TEXT PRIMARY KEY,
    key_data TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
