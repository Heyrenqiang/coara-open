package service

import (
	"fmt"
	"log/slog"
	"strings"

	"gomatrix/internal/utils"

	"golang.org/x/crypto/bcrypt"
)

// EnsurePairingAccount 确保手机配对固定账号存在且密码以配置为准。
// 密码为空时配对账号禁用（QR② 不携带凭据，App 退回随机注册）。
// 固定账号让手机重装/重扫后找回同一账号、同一房间与全部历史。
func EnsurePairingAccount(s *Services) error {
	username := strings.ToLower(strings.TrimSpace(s.Config.Pairing.Username))
	password := s.Config.Pairing.Password
	if username == "" || password == "" {
		return nil
	}
	userID := utils.UserID(username, s.Config.ServerName)

	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return fmt.Errorf("hash pairing password: %w", err)
	}

	exists, err := s.DB.UserExists(userID)
	if err != nil {
		return fmt.Errorf("check pairing user: %w", err)
	}
	if exists {
		if err := s.DB.SetPassword(userID, string(hash)); err != nil {
			return fmt.Errorf("reset pairing password: %w", err)
		}
		slog.Info("pairing account ready (password reset to config)", "user_id", userID)
		return nil
	}
	if err := s.DB.CreateUser(userID, string(hash), false); err != nil {
		return fmt.Errorf("create pairing user: %w", err)
	}
	slog.Info("pairing account registered", "user_id", userID)
	return nil
}
