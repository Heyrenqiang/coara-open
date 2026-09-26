package service

import (
	"fmt"
	"strings"

	"gomatrix/internal/models"
	"gomatrix/internal/utils"
	"golang.org/x/crypto/bcrypt"
)

// UserService handles accounts and devices.
type UserService struct {
	s *Services
}

// NewUserService creates a new user service.
func NewUserService(s *Services) *UserService {
	return &UserService{s: s}
}

// Register creates a user account and returns the user ID.
func (us *UserService) Register(localpart, password string, admin bool) (string, error) {
	localpart = strings.ToLower(localpart)
	if !isValidLocalpart(localpart) {
		return "", fmt.Errorf("invalid localpart")
	}
	userID := utils.UserID(localpart, us.s.Config.ServerName)
	exists, err := us.s.DB.UserExists(userID)
	if err != nil {
		return "", err
	}
	if exists {
		return "", fmt.Errorf("user in use")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return "", err
	}
	if err := us.s.DB.CreateUser(userID, string(hash), admin); err != nil {
		return "", err
	}
	return userID, nil
}

// Login validates credentials and creates a device + access token.
func (us *UserService) Login(localpart, password, deviceID, deviceName string) (userID, token string, finalDeviceID string, err error) {
	localpart = strings.ToLower(localpart)
	userID = utils.UserID(localpart, us.s.Config.ServerName)
	user, err := us.s.DB.GetUser(userID)
	if err != nil {
		return "", "", "", err
	}
	if user == nil {
		return "", "", "", fmt.Errorf("invalid credentials")
	}
	if err := bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(password)); err != nil {
		return "", "", "", fmt.Errorf("invalid credentials")
	}
	if deviceID == "" {
		deviceID = utils.GenerateDeviceID()
	}
	// If device doesn't exist, create it
	dev, err := us.s.DB.GetDevice(deviceID)
	if err != nil {
		return "", "", "", err
	}
	if dev == nil {
		if err := us.s.DB.CreateDevice(deviceID, userID, deviceName); err != nil {
			return "", "", "", err
		}
	} else if dev.UserID != userID {
		return "", "", "", fmt.Errorf("device belongs to another user")
	} else if deviceName != "" {
		_ = us.s.DB.UpdateDevice(deviceID, deviceName)
	}

	token = utils.GenerateToken()
	if err := us.s.DB.CreateAccessToken(token, userID, deviceID); err != nil {
		return "", "", "", err
	}
	return userID, token, deviceID, nil
}

// Logout invalidates an access token and optionally the device.
func (us *UserService) Logout(token string, allDevices bool, userID string) error {
	if allDevices {
		devices, err := us.s.DB.GetDevicesForUser(userID)
		if err != nil {
			return err
		}
		for _, d := range devices {
			if err := us.s.DB.DeleteAccessTokensForDevice(d.DeviceID); err != nil {
				return err
			}
			if err := us.s.DB.DeleteDevice(d.DeviceID); err != nil {
				return err
			}
		}
		return nil
	}
	_, devID, err := us.s.DB.GetTokenUser(token)
	if err != nil {
		return err
	}
	if err := us.s.DB.DeleteAccessToken(token); err != nil {
		return err
	}
	if devID != "" {
		return us.s.DB.DeleteDevice(devID)
	}
	return nil
}

// GetUserByToken validates a token and returns user/device IDs.
func (us *UserService) GetUserByToken(token string) (userID, deviceID string, err error) {
	return us.s.DB.GetTokenUser(token)
}

// GetUser returns a user.
func (us *UserService) GetUser(userID string) (*models.User, error) {
	return us.s.DB.GetUser(userID)
}

// GetDevices returns devices for a user.
func (us *UserService) GetDevices(userID string) ([]*models.Device, error) {
	return us.s.DB.GetDevicesForUser(userID)
}

// UpdateDevice updates a device display name.
func (us *UserService) UpdateDevice(deviceID, displayName string) error {
	return us.s.DB.UpdateDevice(deviceID, displayName)
}

// UserCount returns the total number of registered users.
func (us *UserService) UserCount() (int, error) {
	row := us.s.DB.QueryRow(`SELECT COUNT(*) FROM users`)
	var n int
	err := row.Scan(&n)
	return n, err
}

// DeleteDevice deletes a single device.
func (us *UserService) DeleteDevice(deviceID string) error {
	if err := us.s.DB.DeleteAccessTokensForDevice(deviceID); err != nil {
		return err
	}
	return us.s.DB.DeleteDevice(deviceID)
}

// UpdateProfile updates user profile.
func (us *UserService) UpdateProfile(userID, displayName, avatarURL string) error {
	return us.s.DB.UpdateProfile(userID, displayName, avatarURL)
}

// UpdateLastSeen records last activity for a device.
func (us *UserService) UpdateLastSeen(deviceID, ip string) error {
	return us.s.DB.UpdateDeviceLastSeen(deviceID, ip, utils.NowMillis())
}

func isValidLocalpart(localpart string) bool {
	return utils.ValidateLocalpart(localpart)
}
