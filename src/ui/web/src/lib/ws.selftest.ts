/**
 * Self-test for WS reconnect backoff (infinite reconnect, 30s cap).
 * Run: npx tsx src/lib/ws.selftest.ts
 */
import { reconnectDelayMs } from "./ws.ts";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

// First attempt: base 1s with ±30% jitter → [700, 1300]
{
  const min = reconnectDelayMs(0, () => 0);
  const max = reconnectDelayMs(0, () => 0.999999);
  assert(min === 700, `attempt 0 min should be 700, got ${min}`);
  assert(max <= 1300, `attempt 0 max should be <= 1300, got ${max}`);
}

// Exponential growth: attempt 3 → base 8s → [5600, 10400]
{
  const min = reconnectDelayMs(3, () => 0);
  assert(min === 5600, `attempt 3 min should be 5600, got ${min}`);
}

// Never terminal: arbitrarily large attempt counts still return a delay
// (the old implementation gave up after 10 attempts with a terminal error).
{
  for (const attempts of [10, 20, 100, 10000]) {
    const delay = reconnectDelayMs(attempts);
    assert(Number.isFinite(delay) && delay > 0, `attempt ${attempts} must still schedule a retry`);
    assert(delay <= 30_000, `attempt ${attempts} must cap at 30s, got ${delay}`);
  }
}

// Cap keeps jitter below 30s: at the cap the delay is in [21000, 30000]
{
  const min = reconnectDelayMs(100, () => 0);
  const max = reconnectDelayMs(100, () => 0.999999);
  assert(min === 21_000, `capped min should be 21000, got ${min}`);
  assert(max === 30_000, `capped max should be 30000, got ${max}`);
}

console.log("ws.selftest: OK");
