package db

import (
	"database/sql"
	"time"

	"gomatrix/internal/models"
)

// CreateMedia stores media metadata.
func (db *DB) CreateMedia(media *models.Media) error {
	return db.InTransaction(func(tx *sql.Tx) error {
		return db.CreateMediaTx(tx, media)
	})
}

// CreateMediaTx stores media metadata inside an open transaction.
func (db *DB) CreateMediaTx(tx *sql.Tx, media *models.Media) error {
	_, err := tx.Exec(
		`INSERT INTO media (media_id, sha256, content_type, size, file_path, created_at)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		media.MediaID, media.SHA256, media.ContentType, media.Size, media.FilePath, media.CreatedAt.UnixMilli(),
	)
	return err
}

// GetMedia retrieves media metadata by media ID.
func (db *DB) GetMedia(mediaID string) (*models.Media, error) {
	row := db.QueryRow(
		`SELECT media_id, sha256, content_type, size, file_path, created_at FROM media WHERE media_id = ?`,
		mediaID,
	)
	m := &models.Media{}
	var created int64
	err := row.Scan(&m.MediaID, &m.SHA256, &m.ContentType, &m.Size, &m.FilePath, &created)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	m.CreatedAt = time.UnixMilli(created)
	return m, nil
}

// GetMediaBySHA256 retrieves media by its content hash (for dedup).
func (db *DB) GetMediaBySHA256(sha256 string) (*models.Media, error) {
	row := db.QueryRow(
		`SELECT media_id, sha256, content_type, size, file_path, created_at FROM media WHERE sha256 = ? LIMIT 1`,
		sha256,
	)
	m := &models.Media{}
	var created int64
	err := row.Scan(&m.MediaID, &m.SHA256, &m.ContentType, &m.Size, &m.FilePath, &created)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	m.CreatedAt = time.UnixMilli(created)
	return m, nil
}
