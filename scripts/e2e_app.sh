#!/usr/bin/env bash
# Layer 1 of the Onça store-app E2E (#259): /exec in APP MODE (the shell's window.__ONCA_APP__ +
# a recording ReactNativeWebView) in WebKit — Chromium if WebKit can't launch — against PRODUCTION,
# signed in as the QA persona (Secrets Manager, read in-process). Tests: tests/e2e_app/.
#
#   scripts/e2e_app.sh                      run the suite now (screenshots in /tmp/onca-e2e/)
#   scripts/e2e_app.sh -k gate              extra args go to pytest
#   scripts/e2e_app.sh --if-deployed        run only if the live site/stack changed since the last
#                                           run AND has been stable for 15 min (post-deploy cron)
#   scripts/e2e_app.sh --setup-webkit       fetch WebKit's missing host libraries (no sudo:
#                                           apt-get download + dpkg -x into ~/.cache/onca-wkdeps)
#
# Env: ONCA_E2E_BASE_URL (default https://onssa.org), ONCA_E2E_BROWSER (webkit|chromium|auto),
#      ONCA_E2E_PERSONA (default admin), ONCA_E2E_OUT (default /tmp/onca-e2e), AWS_PROFILE (my2027).
# Logs + JUnit XML: ~/.cache/onca-e2e/ (last-status holds the latest one-line result).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
WK="${ONCA_E2E_WKDEPS:-$HOME/.cache/onca-wkdeps}"
STATE="${ONCA_E2E_STATE:-$HOME/.cache/onca-e2e}"
export AWS_PROFILE="${AWS_PROFILE:-my2027}"
export ONCA_E2E=1
mkdir -p "$STATE/logs"

# Ubuntu 26.04 package names for the libraries Playwright's WebKit build needs but the host lacks
# (the browser itself is in ~/.cache/ms-playwright). glib-networking provides TLS to WebKit.
WK_PKGS="libgtk-4-1 libgraphene-1.0-0 libxslt1.1 libevent-2.1-7t64 libopus0 libgstreamer1.0-0
 libgstreamer-plugins-base1.0-0 libgstreamer-gl1.0-0 libgstreamer-plugins-bad1.0-0
 libgstreamer-plugins-extra1.0-0 libflite1 libwebpdemux2 libavif16 libharfbuzz-icu0 libwebpmux3
 libwebp7 libwayland-server0 libmanette-0.2-0 libenchant-2-2 libhyphen0 libsecret-1-0 libgles2
 libglvnd0 libx264-165 libdav1d7 libgav1-2 libyuv0 libtiff6 liborc-0.4-0t64 libgudev-1.0-0 libevdev2
 libhidapi-hidraw0 libharfbuzz-subset0 libbacktrace0 libcairo-script-interpreter2 libabsl20260107
 libdeflate0 libjbig0 liblerc4 glib-networking glib-networking-common libproxy1v5
 gsettings-desktop-schemas"

setup_webkit() {
  mkdir -p "$WK/debs" "$WK/root"
  ( cd "$WK/debs" && for p in $WK_PKGS; do
      ls "${p}"_*.deb >/dev/null 2>&1 || apt-get download "$p" >/dev/null 2>&1 || echo "WARN: could not download $p"
    done )
  for d in "$WK"/debs/*.deb; do dpkg -x "$d" "$WK/root"; done
  ln -sf libx264.so.165 "$WK/root/usr/lib/x86_64-linux-gnu/libx264.so"
  echo "WebKit host libs in $WK/root"
}

# The deployed state: the shell files' ETags + the app stack's last update. Prints one line.
fingerprint() {
  "$PY" -I - <<'PYEOF'
import os, urllib.request, boto3
base = os.environ.get("ONCA_E2E_BASE_URL", "https://onssa.org").rstrip("/")
parts = []
for p in ("/exec", "/v2/context.js", "/v2/app.js", "/v2/app.css", "/exec-sw.js"):
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + p, method="HEAD",
                                   headers={"user-agent": "onca-e2e-app/1"}), timeout=20)
        parts.append(r.headers.get("etag") or r.headers.get("last-modified") or "?")
    except Exception as e:
        parts.append("err")
try:
    st = boto3.Session().client("cloudformation", region_name="us-east-1").describe_stacks(
        StackName="OncaPrototypeStack")["Stacks"][0]
    parts.append(str(st.get("LastUpdatedTime") or st.get("CreationTime")))
except Exception:
    parts.append("cfn?")
print("|".join(parts).replace(" ", "T"))
PYEOF
}

if [ "${1:-}" = "--setup-webkit" ]; then setup_webkit; exit 0; fi

if [ "${1:-}" = "--if-deployed" ]; then
  shift
  fp="$(fingerprint)"
  [ "$fp" = "$(cat "$STATE/last-run-fingerprint" 2>/dev/null)" ] && exit 0
  case "$fp" in *err*|*"cfn?"*) exit 0 ;; esac          # can't tell: try again next tick
  now=$(date +%s)
  read -r pfp pts < <(cat "$STATE/pending" 2>/dev/null || echo "- 0")
  if [ "$fp" != "$pfp" ]; then echo "$fp $now" > "$STATE/pending"; exit 0; fi
  [ $((now - pts)) -lt 900 ] && exit 0                  # still settling (multi-step deploys)
  RUN_FP="$fp"
fi

# WebKit's missing libs, if fetched (else the suite falls back to Chromium)
if [ -d "$WK/root/usr/lib/x86_64-linux-gnu" ]; then
  export LD_LIBRARY_PATH="$WK/root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export GIO_EXTRA_MODULES="$WK/root/usr/lib/x86_64-linux-gnu/gio/modules"
  export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
fi

exec 9>"$STATE/lock"
flock -n 9 || { echo "another e2e_app run is in progress"; exit 0; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log="$STATE/logs/e2e-$stamp.log"
t0=$(date +%s)
cd "$ROOT" && "$PY" -m pytest tests/e2e_app -v -rA -p no:cacheprovider \
  --junitxml="$STATE/logs/e2e-$stamp.xml" "$@" 2>&1 | grep -v "PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS" | tee "$log"
rc=${PIPESTATUS[0]}
secs=$(( $(date +%s) - t0 ))
summary="$(grep -E '^=+ .*(passed|failed|error).* =+$' "$log" | tail -1 | tr -d '=' | xargs)"
echo "$stamp rc=$rc ${secs}s ${summary:-no summary} log=$log" | tee "$STATE/last-status"
[ -n "${RUN_FP:-}" ] && echo "$RUN_FP" > "$STATE/last-run-fingerprint"
ls -1t "$STATE"/logs/e2e-* 2>/dev/null | tail -n +61 | xargs -r rm -f   # keep the last 30 runs
exit "$rc"
