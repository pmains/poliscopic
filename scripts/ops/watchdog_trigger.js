// Condition watcher for the poliscopic.com 15-minute uptime automation.
//
// Design notes (2026-09-15 false positive):
//   A probe result is only usable when curl reports a three-digit status code.
//   Empty or unparseable output means the *measurement* failed (a dropped exec
//   result), not that the site is down, so it is inconclusive and never fires.
//   Firing requires two usable measurements, both non-200, spaced 4s apart, and
//   is rate-limited to one alert per 30 minutes.
const res = await exec({
  command:
    "for i in 1 2; do curl -s -o /dev/null -w '%{http_code} ' -m 15 https://poliscopic.com/; sleep 4; done",
});
const codes = String((res && res.aggregated) || "")
  .trim()
  .split(/\s+/)
  .filter((c) => /^[0-9]{3}$/.test(c));
const failures = codes.filter((c) => c !== "200");
const now = Date.now();
const last = Number((trigger.state && trigger.state.lastFire) || 0);
const QUIET_MS = 30 * 60 * 1000;
if (codes.length < 2 || failures.length < 2) {
  json({
    fire: false,
    state: {
      status: "ok-or-inconclusive",
      codes: codes.join(","),
      lastFire: last,
      checked: now,
    },
  });
} else if (now - last >= QUIET_MS) {
  json({
    fire: true,
    message:
      "poliscopic.com failed 2/2 probes (" +
      codes.join(",") +
      ") at " +
      new Date().toISOString(),
    state: { status: "down", codes: codes.join(","), lastFire: now, checked: now },
  });
} else {
  json({
    fire: false,
    state: {
      status: "down-suppressed",
      codes: codes.join(","),
      lastFire: last,
      checked: now,
    },
  });
}
