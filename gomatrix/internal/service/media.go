package service

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"gomatrix/internal/models"
	"gomatrix/internal/utils"
)

// MediaService handles uploads and downloads.
type MediaService struct {
	s *Services
}

// NewMediaService creates a media service.
func NewMediaService(s *Services) *MediaService {
	return &MediaService{s: s}
}

// Upload stores uploaded media and returns MXC info.
func (ms *MediaService) Upload(data []byte, contentType, filename string) (*models.Media, error) {
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])

	// Dedup by hash
	existing, err := ms.s.DB.GetMediaBySHA256(hash)
	if err != nil {
		return nil, err
	}
	if existing != nil {
		return existing, nil
	}

	mediaID := utils.GenerateMediaID()
	if err := os.MkdirAll(ms.s.Config.MediaPath, 0o755); err != nil {
		return nil, err
	}
	filePath := filepath.Join(ms.s.Config.MediaPath, mediaID)
	if err := os.WriteFile(filePath, data, 0o644); err != nil {
		return nil, err
	}

	media := &models.Media{
		MediaID:     mediaID,
		SHA256:      hash,
		ContentType: utils.DefaultIfEmpty(contentType, utils.ContentTypeForFile(filename)),
		Size:        int64(len(data)),
		FilePath:    filePath,
		CreatedAt:   time.Now(),
	}
	if err := ms.s.DB.CreateMedia(media); err != nil {
		_ = os.Remove(filePath)
		return nil, err
	}
	return media, nil
}

// GetMedia retrieves media metadata and file path.
func (ms *MediaService) GetMedia(mediaID string) (*models.Media, []byte, error) {
	media, err := ms.s.DB.GetMedia(mediaID)
	if err != nil {
		return nil, nil, err
	}
	if media == nil {
		return nil, nil, fmt.Errorf("media not found")
	}
	data, err := os.ReadFile(media.FilePath)
	if err != nil {
		return nil, nil, err
	}
	return media, data, nil
}
