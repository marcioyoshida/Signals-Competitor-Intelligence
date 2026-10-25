#!/usr/bin/env bash
# CI only (.github/workflows/mobile-qa.yml): run each signed-in flow on its own, so one failure
# doesn't hide the others. Credentials come from MAESTRO_QA_* in the environment (never argv).
# On a failure, print the screen's text to the job log, where GitHub masks the secrets. The
# debug output (maestro.log holds typed text) stays on the runner; the repo is public.
set -u
rc=0
dump() {
  echo "::group::$1: screen text at the failure"
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null
  adb exec-out cat /sdcard/ui.xml | grep -oE '(text|content-desc)="[^"]+"' | sort -u
  echo "::endgroup::"
}
run() { maestro test "mobile/apps/onca/maestro/$1.yaml" --format junit --output "maestro-$2.xml" --debug-output maestro-debug/auth; }
CARD="https://onssa.org/exec?c=0000000000000000"
for flow in "$@"; do
  if [ "$flow" = card_link ]; then
    # #289: warm (app running -> Linking "url" event), then cold (force-stopped -> getInitialURL)
    if run card_link card_link-setup \
       && adb shell am start -W -a android.intent.action.VIEW -d "'$CARD'" -n org.onssa.onca/.MainActivity >/dev/null \
       && run card_link_check card_link-warm \
       && adb shell am force-stop org.onssa.onca \
       && adb shell am start -W -a android.intent.action.VIEW -d "'$CARD'" -n org.onssa.onca/.MainActivity >/dev/null \
       && run card_link_check card_link-cold; then :; else rc=1; dump card_link; fi
    continue
  fi
  if ! maestro test "mobile/apps/onca/maestro/$flow.yaml" --format junit --output "maestro-$flow.xml" --debug-output maestro-debug/auth; then
    rc=1
    dump "$flow"
    adb shell cmd connectivity airplane-mode disable >/dev/null 2>&1 || true   # a flow that failed offline
  fi
done
exit $rc
