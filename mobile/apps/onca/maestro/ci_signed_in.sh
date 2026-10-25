#!/usr/bin/env bash
# CI only (.github/workflows/mobile-qa.yml): run each signed-in flow on its own, so one failure
# doesn't hide the others. Credentials come from MAESTRO_QA_* in the environment (never argv).
# On a failure, print the screen's text to the job log, where GitHub masks the secrets. The
# debug output (maestro.log holds typed text) stays on the runner; the repo is public.
set -u
rc=0
for flow in "$@"; do
  if ! maestro test "mobile/apps/onca/maestro/$flow.yaml" --format junit --output "maestro-$flow.xml" --debug-output maestro-debug/auth; then
    rc=1
    echo "::group::$flow: screen text at the failure"
    adb shell uiautomator dump /sdcard/ui.xml >/dev/null
    adb exec-out cat /sdcard/ui.xml | grep -oE '(text|content-desc)="[^"]+"' | sort -u
    echo "::endgroup::"
    adb shell cmd connectivity airplane-mode disable >/dev/null 2>&1 || true   # a flow that failed offline
  fi
done
exit $rc
