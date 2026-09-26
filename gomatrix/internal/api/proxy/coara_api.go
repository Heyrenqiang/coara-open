// Package proxy reverse-proxies /coara-api/* to the local coara web server's
// REST API, injecting the dashboard token so mobile clients only need their
// Matrix access token. The coara web server stays bound to localhost; this
// proxy is the only entry point and is itself gated by Matrix AuthMiddleware.
package proxy

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httputil"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// CoaraAPIProxy proxies /coara-api/* (prefix already stripped by chi Mount)
// to http://127.0.0.1:<port>/api/* with X-Coara-Token injected.
type CoaraAPIProxy struct {
	upstreamHost string
	tokenFile    string
	rewritePath  func(string) string
	proxy        *httputil.ReverseProxy

	mu         sync.Mutex
	token      string
	tokenMtime time.Time
}

// NewCoaraAPIProxy builds the proxy. tokenFile may be empty, in which case it
// defaults to $COARA_HOME/system/dashboard_token.
func NewCoaraAPIProxy(upstreamPort int, tokenFile string) *CoaraAPIProxy {
	if upstreamPort <= 0 {
		upstreamPort = 8080
	}
	return newCoaraAPIProxy("127.0.0.1:"+strconv.Itoa(upstreamPort), tokenFile)
}

// NewCoaraWebProxy reverse-proxies everything gomatrix 自身未认领的路径
// （SPA 页面 / 静态资源 / /api/* / /ws WebSocket）到本地 coara web server，
// 路径原样透传、注入 dashboard token。手机端经它加载工作流编辑器 SPA——
// coara web server 仍绑定 loopback，本代理挂 Matrix AuthMiddleware 之后，
// 是唯一入口。Go ReverseProxy 原生支持 WebSocket Upgrade。
func NewCoaraWebProxy(upstreamPort int, tokenFile string) *CoaraAPIProxy {
	if upstreamPort <= 0 {
		upstreamPort = 8080
	}
	p := newCoaraAPIProxy("127.0.0.1:"+strconv.Itoa(upstreamPort), tokenFile)
	p.rewritePath = func(path string) string { return path }
	return p
}

// newCoaraAPIProxy lets tests point the proxy at an httptest upstream.
func newCoaraAPIProxy(upstreamHost, tokenFile string) *CoaraAPIProxy {
	if tokenFile == "" {
		if home := os.Getenv("COARA_HOME"); home != "" {
			tokenFile = filepath.Join(home, "system", "dashboard_token")
		}
	}
	p := &CoaraAPIProxy{
		upstreamHost: upstreamHost,
		tokenFile:    tokenFile,
		rewritePath: func(path string) string {
			// chi v5 Mount 只改路由上下文，不改写裸 http.Handler 看到的
			// req.URL.Path，/coara-api 前缀需在这里自行剥离，
			// 否则上游收到 /api/coara-api/... → 404
			return "/api" + strings.TrimPrefix(path, "/coara-api")
		},
	}
	p.proxy = &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL.Scheme = "http"
			req.URL.Host = p.upstreamHost
			req.URL.Path = p.rewritePath(req.URL.Path)
			req.Host = p.upstreamHost
			p.mu.Lock()
			token := p.token
			p.mu.Unlock()
			if token != "" {
				req.Header.Set("X-Coara-Token", token)
			}
		},
		Transport: &http.Transport{
			DialContext:           (&net.Dialer{Timeout: 3 * time.Second}).DialContext,
			ResponseHeaderTimeout: 60 * time.Second,
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "pc_unreachable"})
		},
	}
	return p
}

// ServeHTTP refreshes the dashboard token (lazy, mtime-checked) then proxies.
func (p *CoaraAPIProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if err := p.refreshToken(); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "dashboard_token_unavailable"})
		return
	}
	p.proxy.ServeHTTP(w, r)
}

// refreshToken reloads the token file when its mtime changes. The file is
// created by coara on first web-server start, which may be after gomatrix, so
// loading is lazy rather than at startup.
func (p *CoaraAPIProxy) refreshToken() error {
	if p.tokenFile == "" {
		return os.ErrNotExist
	}
	info, err := os.Stat(p.tokenFile)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if !info.ModTime().After(p.tokenMtime) && p.token != "" {
		return nil
	}
	raw, err := os.ReadFile(p.tokenFile)
	if err != nil {
		return err
	}
	token := strings.TrimSpace(string(raw))
	if token == "" {
		return os.ErrInvalid
	}
	p.token = token
	p.tokenMtime = info.ModTime()
	return nil
}

func writeJSON(w http.ResponseWriter, status int, payload map[string]string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
