---
name: verify-admin-spa
description: Launch and visually verify the DevCake admin SPA at responsive viewports.
---

# Admin SPA verification

1. Ensure the existing admin/backend stack answers `http://127.0.0.1:8080/nginx-health`.
2. Install dependencies with `npm --prefix admin/spa ci --no-audit --no-fund`.
3. Start Vite on `127.0.0.1:5199` with `ADMIN_USER` and `ADMIN_PASSWORD` matching the running admin container so its `/api` proxy authenticates.
4. Set `UI_CHROME` to the local Chrome/Chromium executable and run:

```bash
UI_BASE=http://127.0.0.1:5199 UI_CHROME="/absolute/path/to/chrome" \
  node admin/spa/tests/capture-responsive.mjs
```

5. Inspect the generated files in `docs/img/admin-responsive-remediation/` and retain the script's geometry/probe output as evidence.

Key gotchas:
- The app scrolls inside `<main>`; viewport screenshots are more useful than `fullPage` captures.
- Hash-only navigation does not remount the SPA, so screenshot automation must reload after navigation.
- Missions data may be unavailable from the configured PMO. The capture script intercepts only Missions/run fixture requests and leaves the real local shell/config/backend active.
- Bind Vite explicitly to `127.0.0.1`; `localhost` may resolve only to IPv6 while the browser harness targets IPv4.
