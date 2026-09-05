# Changelog

## 0.4.1 — 2026-09-02

- Restrict privileged sign-in navigation to the exact `substack.com` and
  `www.substack.com` origins, blocking session-bearing navigation to
  publication subdomains.
- Connect custom-domain RSS requests directly to the one validated DNS address
  set, verify the connected peer, and retain TLS SNI and hostname validation.
- Anchor private state operations to verified directory descriptors with
  no-follow, nonblocking regular-file checks, bounded reads, durable atomic
  writes, and strict ownership and permission requirements.
- Replace the panel's direct state-file reader with a bounded, schema-checked
  backend snapshot channel.
- Bound account collections and every persisted/displayed string, rebuild
  loaded state from a strict allowlisted schema, and send private notification
  text directly over D-Bus instead of process arguments.

## 0.4.0 — 2026-09-01

- Move from the legacy `aaron.substack` identity to the GitHub-namespaced
  `0x4a756e65.omarchy-substack` replacement plugin.
- Poll Substack publications with custom domains directly instead of following
  their canonical feed redirects.
- Require the custom domain from authenticated account metadata, Substack's
  DNS target, public IP addresses, and matching Substack response identity.
- Preserve article identities and reset cache validators when a publication
  moves between its Substack and custom-domain feed origins.

## 0.3.2 — 2026-08-31

- Follow the bar host's transparency-aware foreground for the bar glyph and
  ticker so they keep contrast against the wallpaper when the bar is
  transparent, matching the native widgets.

## 0.3.1 — 2026-08-30

- Replace regular-expression HTML cleanup with structured parsing so malformed
  script and style end tags cannot leak active-content text into excerpts.

## 0.3.0 — 2026-08-30

- Reject every backend redirect before it can change origin, forward a session
  cookie, downgrade HTTPS, or reach a private network through an RSS hop.
- Restrict authenticated requests to the exact Substack HTTPS origin and
  validate cookies before constructing request headers.
- Allowlist login-window navigation, deny web permissions, show the current
  origin, and prevent duplicate sign-in windows.
- Render remote feed and account content as plain text and restrict publication
  artwork to known Substack media origins.
- Preserve the last good feed until an unexpected empty subscription response
  is confirmed by a second sync.
- Move plugin IPC to the singleton service, add daemon restart backoff, and
  surface authentication-process errors in the panel.
- Keep the newest post scrolling in the bar after it has been read.
- Add security regression coverage, Python 3.13/3.14 CI, CodeQL, immutable
  Action pins, Dependabot, signed release support, and a marketplace preview.

## 0.2.1 — 2026-08-29

- Reserve explicit gutters for feed and settings scrollbars.
- Replace implicit footer layout with fixed anchors.
- Clarify that the headline toggle affects Substack, not Spotmarchy music.
- Verify headline-setting persistence through the running Omarchy shell.

## 0.2.0 — 2026-08-29

- Add a full in-panel settings and account surface.
- Add password-first, email-link, back, and reload authentication controls.
- Exclude publications administered by the reader by default.
- Replace generated initials with real Substack publication artwork.
- Add Last-Modified feed validation alongside ETag support.
- Support additional subscription endpoint response shapes.
- Make concurrent settings updates process-safe.
- Simplify feed status labels and correct narrow-panel alignment.

## 0.1.0 — 2026-08-29

- Initial authenticated subscription discovery, RSS polling, native
  notifications, unread tracking, and browser handoff.
