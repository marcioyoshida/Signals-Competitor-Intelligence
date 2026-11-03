# Onça app changelog

Store builds of the native shell. The web app (`/exec`) ships with every site deploy and is not
listed here. Release steps: `docs/mobile-release-runbook.md`.

## 0.2.0 (unreleased)
- **#311:** OTA updates (`expo-updates`, `runtimeVersion: appVersion`; channels `preview`,
  `production`, `e2e`) and a quota-guarded manual release workflow (`mobile-release`).
- **#312:** a fatal JS error is flagged on the device and reported on the next launch
  (`/api/session/app`, kind `fatal`; enums, version and build only). Bridge 4 passes the version
  and build to the page.
- **#318:** `/exec` checks the minimum supported version (soft banner, hard block).
- The user agent is `OncaApp/<version>` (was a fixed `OncaApp/0.1`).

## 0.1.0
- **Android APKs** (internal): 41cf7f4a, 11e0322a (#258), 8ada5bb3 (#256/#257). Later fixes
  shipped in the next builds: cookie flush (f0ca1b6), back handling (6f9bf9e), recents privacy
  (5e902b6), `/c/` links (bridge 3, 1eb822c).
- **iOS (4)**, EAS e7b29d1e (2026-10-12): the "Login não concluído" message and the sign-in
  outcome beacon.
- **iOS (3)**, EAS 410849cd (2026-10-12): the first TestFlight build.
