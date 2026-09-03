#!/bin/bash
# Remove Playwright temp dirs orphaned by a crashed/killed browser (>60 min old). A live browsers
# profile is always younger (idle-close kills the browser after 10 min). Used by the wrapper (at
# each MCP start) and the daily systemd timer. Errors ignored.
find /tmp -maxdepth 1 -name "playwright-artifacts-*" -mmin +60 -exec rm -rf {} + 2>/dev/null
find /tmp -maxdepth 1 -name "playwright_chromiumdev_profile-*" -mmin +60 -exec rm -rf {} + 2>/dev/null
exit 0
