package service

import (
	"context"
	"log/slog"
	"time"

	"gomatrix/internal/utils"
)

// 宝箱密码回信只用于实时投递（Python vault_bridge 等待窗口 60s），
// sync 投递走读库回放无法不落盘，故超窗后从库中删除，
// 避免主密码长期明文留在 SQLite（REMAINING_ISSUES #1 过渡缓解）。
const (
	vaultReplyRetention     = 5 * time.Minute
	vaultReplySweepInterval = time.Minute
)

// StartVaultReplySweeper 启动即清一次存量（覆盖历史库），之后定期删除超窗回信。
func (s *Services) StartVaultReplySweeper(ctx context.Context) {
	s.sweepVaultReplies()
	go func() {
		ticker := time.NewTicker(vaultReplySweepInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.sweepVaultReplies()
			}
		}
	}()
}

func (s *Services) sweepVaultReplies() {
	cutoff := utils.NowMillis() - vaultReplyRetention.Milliseconds()
	n, err := s.DB.DeleteVaultReplyEvents(cutoff)
	if err != nil {
		slog.Warn("vault reply sweep failed", "error", err)
	} else if n > 0 {
		slog.Info("vault reply events purged", "count", n)
	}
}
