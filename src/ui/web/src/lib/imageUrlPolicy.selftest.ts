/**
 * Self-test for markdown image URL whitelist (same-origin or trusted hosts).
 * Run: npx tsx src/lib/imageUrlPolicy.selftest.ts
 */
import { isTrustedImageUrl } from "./imageUrlPolicy.ts";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

const PAGE = "http://localhost:8000";

// 同源绝对地址：放行
{
  assert(isTrustedImageUrl("http://localhost:8000/media/abc.png", PAGE), "same origin absolute");
}

// 相对路径：解析后落在页面同源，放行
{
  assert(isTrustedImageUrl("/media/abc.png", PAGE), "root-relative path");
  assert(isTrustedImageUrl("media/abc.png", PAGE), "bare relative path");
}

// 外部任意 URL：拦截
{
  assert(!isTrustedImageUrl("https://evil.example.com/track.png?x=1", PAGE), "external https");
  assert(!isTrustedImageUrl("http://attacker.local/a.png", PAGE), "external http");
}

// 内网探测：拦截
{
  assert(!isTrustedImageUrl("http://192.168.1.1/admin.png", PAGE), "lan ip probe");
  assert(!isTrustedImageUrl("http://169.254.169.254/latest.png", PAGE), "metadata probe");
}

// 同主机不同端口/scheme 不算同源：拦截
{
  assert(!isTrustedImageUrl("http://localhost:9000/x.png", PAGE), "different port");
  assert(!isTrustedImageUrl("https://localhost:8000/x.png", PAGE), "different scheme");
}

// 默认端口归一化：显式 80 与默认同源
{
  assert(
    isTrustedImageUrl("http://example.com:80/x.png", "http://example.com"),
    "default port normalized",
  );
}

// 非 http/https 协议：拦截
{
  assert(!isTrustedImageUrl("data:image/png;base64,AAAA", PAGE), "data uri");
  assert(!isTrustedImageUrl("file:///etc/passwd", PAGE), "file uri");
  assert(!isTrustedImageUrl("javascript:alert(1)", PAGE), "javascript uri");
}

// 非法/空 URL：拦截（带空格的 host 解析失败；空串无路径）
{
  assert(!isTrustedImageUrl("", PAGE), "empty url");
  assert(!isTrustedImageUrl("http://exa mple.com/x.png", PAGE), "malformed host");
}

// 无法识别的垃圾串按相对路径解析到页面同源：放行（请求只会打到自家服务，无外带）
{
  assert(isTrustedImageUrl("ht tp://broken", PAGE), "garbage resolves same-origin");
}

// userinfo 伪装：host 仍是 evil，拦截
{
  assert(!isTrustedImageUrl("http://localhost:8000@evil.example.com/x.png", PAGE), "userinfo spoof");
}

// 受信域名白名单：精确与子域放行，形似域名不放行
{
  const trusted = ["cdn.example.com"];
  assert(
    isTrustedImageUrl("https://cdn.example.com/a.png", PAGE, trusted),
    "exact trusted host",
  );
  assert(
    isTrustedImageUrl("https://img.cdn.example.com/a.png", PAGE, trusted),
    "subdomain of trusted host",
  );
  assert(
    !isTrustedImageUrl("https://evilcdn.example.com/a.png", PAGE, trusted),
    "lookalike host rejected",
  );
  assert(
    !isTrustedImageUrl("https://cdn.example.com.evil.com/a.png", PAGE, trusted),
    "suffix spoof rejected",
  );
}

// 页面 origin 非法：一律拦截
{
  assert(!isTrustedImageUrl("http://localhost:8000/x.png", "not-a-url"), "bad page origin");
  assert(!isTrustedImageUrl("/x.png", ""), "empty page origin");
}

console.log("imageUrlPolicy selftest: all 15 groups passed");
