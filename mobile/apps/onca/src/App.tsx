// ADR 029 (#254 spike → #255): the Onça store app is the /exec PWA in a WebView. The native jobs:
// sign-in in the SYSTEM browser (Google refuses embedded WebViews), external links out to the
// system browser, Android back, native push (#256), app lock + app-switcher privacy (#257), native
// share (#258). Everything else is the web app.
import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, BackHandler, Linking, Platform, Pressable, Share, StatusBar, StyleSheet, Text, View } from "react-native";
import { WebView } from "react-native-webview";
import type { WebViewMessageEvent } from "react-native-webview";
import type { ShouldStartLoadRequest } from "react-native-webview/lib/WebViewTypes";
import * as WebBrowser from "expo-web-browser";
import * as Notifications from "expo-notifications";
import * as LocalAuthentication from "expo-local-authentication";
import * as SecureStore from "expo-secure-store";
import * as ScreenCapture from "expo-screen-capture";
import Constants from "expo-constants";
import {
  ORIGIN, REDIRECT, exchangeUrl, injectedFlag, isPurchaseUrl, parseAuthRedirect, parseMessage,
  replyScript, shouldLock, staysInApp,
} from "./bridge.ts";

const BG = "#0e1214";
const ALERTS_TARGET = `${ORIGIN}/exec#hoje-alertas`;
const LOCK_KEY = "onca.lock";          // {enabled, minutes} — a preference, not a secret

// #256: a push shows while the app is open too; Android needs its channel before the first push.
Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false }),
});
if (Platform.OS === "android") {
  Notifications.setNotificationChannelAsync("alerts", {
    name: "Alertas", importance: Notifications.AndroidImportance.HIGH,
  }).catch(() => {});
}

async function pushToken(request: boolean): Promise<{ token?: string; reason?: string }> {
  let { status } = await Notifications.getPermissionsAsync();
  if (status !== "granted" && request) status = (await Notifications.requestPermissionsAsync()).status;
  if (status !== "granted") return { reason: "permissão negada no aparelho" };
  try {
    const projectId = (Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined)?.eas?.projectId;
    const t = await Notifications.getExpoPushTokenAsync({ projectId });
    return { token: t.data };
  } catch {
    // Android without FCM credentials in this build, or an iOS simulator: no device token
    return { reason: "alertas indisponíveis neste aparelho" };
  }
}

type Lock = { enabled: boolean; minutes: number };
async function readLock(): Promise<Lock> {
  try {
    const v = JSON.parse((await SecureStore.getItemAsync(LOCK_KEY)) || "null");
    if (v && typeof v.enabled === "boolean") return { enabled: v.enabled, minutes: Number(v.minutes) || 5 };
  } catch { /* fall through */ }
  return { enabled: false, minutes: 5 };
}
async function lockAvailable(): Promise<boolean> {
  return (await LocalAuthentication.getEnrolledLevelAsync()) !== LocalAuthentication.SecurityLevel.NONE;
}
async function authenticate(): Promise<boolean> {
  const r = await LocalAuthentication.authenticateAsync({ promptMessage: "Desbloquear o Onça", disableDeviceFallback: false });
  return r.success;
}

export default function App() {
  const web = useRef<WebView>(null);
  const canGoBack = useRef(false);
  const lock = useRef<Lock>({ enabled: false, minutes: 5 });
  const backgroundedAt = useRef<number | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [locked, setLocked] = useState(false);
  const [covered, setCovered] = useState(false);     // app-switcher privacy screen

  // navigate INSIDE the same WebView (keeps sessionStorage, where /exec left the PKCE verifier)
  const go = useCallback((url: string) => {
    web.current?.injectJavaScript(`location.replace(${JSON.stringify(url)}); true;`);
  }, []);
  const reply = useCallback((id: number, value: unknown) => web.current?.injectJavaScript(replyScript(id, value)), []);

  const unlock = useCallback(async () => { if (await authenticate()) setLocked(false); }, []);

  // #257: restore the lock preference; a cold start with the lock on starts locked
  useEffect(() => {
    readLock().then((l) => {
      lock.current = l;
      if (l.enabled) { setLocked(true); ScreenCapture.preventScreenCaptureAsync("lock").catch(() => {}); }
    });
  }, []);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (st) => {
      if (st === "active") {
        setCovered(false);
        if (shouldLock(lock.current.enabled, lock.current.minutes, backgroundedAt.current, Date.now())) setLocked(true);
        backgroundedAt.current = null;
      } else {
        setCovered(true);                               // iOS snapshots on "inactive"
        if (st === "background" && backgroundedAt.current === null) backgroundedAt.current = Date.now();
      }
    });
    return () => sub.remove();
  }, []);

  // #256: a tap on a notification opens Hoje › alertas (cold start included)
  useEffect(() => {
    Notifications.getLastNotificationResponseAsync().then((r) => { if (r) go(ALERTS_TARGET); }).catch(() => {});
    const sub = Notifications.addNotificationResponseReceivedListener(() => go(ALERTS_TARGET));
    return () => sub.remove();
  }, [go]);

  const onMessage = useCallback(async (e: WebViewMessageEvent) => {
    const m = parseMessage(e.nativeEvent.data);
    if (!m) return;
    switch (m.type) {
      case "share":
        Share.share({ message: m.url, url: m.url }).catch(() => {});
        return;
      case "push:token":
        reply(m.id, await pushToken(m.request));
        return;
      case "lock:get":
        reply(m.id, { ...lock.current, available: await lockAvailable() });
        return;
      case "lock:set": {
        if (!(await lockAvailable())) { reply(m.id, { ok: false, reason: "defina um bloqueio de tela no aparelho primeiro" }); return; }
        if (!(await authenticate())) { reply(m.id, { ok: false, reason: "autenticação cancelada" }); return; }
        lock.current = { enabled: m.enabled, minutes: m.minutes };
        await SecureStore.setItemAsync(LOCK_KEY, JSON.stringify(lock.current));
        // Android's app-switcher thumbnail is only reliably hidden with FLAG_SECURE (also blocks
        // screenshots), so it is on exactly while the lock is on.
        if (m.enabled) ScreenCapture.preventScreenCaptureAsync("lock").catch(() => {});
        else ScreenCapture.allowScreenCaptureAsync("lock").catch(() => {});
        reply(m.id, { ok: true, ...lock.current });
        return;
      }
      case "auth":
      case "logout": {
        const r = await WebBrowser.openAuthSessionAsync(m.url, REDIRECT, { preferEphemeralSession: m.type === "logout" });
        if (m.type === "logout") { go(`${ORIGIN}/exec`); return; }
        if (r.type !== "success") return;                // cancelled: stay on the gate
        const res = parseAuthRedirect(r.url);
        if (res.kind === "code") go(exchangeUrl(res.code));
      }
    }
  }, [go, reply]);

  const onShouldStart = useCallback((req: ShouldStartLoadRequest) => {
    if (isPurchaseUrl(req.url)) return false;            // #258: never, not even in the system browser
    if (staysInApp(req.url)) return true;
    if (/^(https?|mailto|tel):/i.test(req.url)) Linking.openURL(req.url).catch(() => {});
    return false;
  }, []);

  useEffect(() => {
    if (Platform.OS !== "android") return;
    const sub = BackHandler.addEventListener("hardwareBackPress", () => {
      if (canGoBack.current) { web.current?.goBack(); return true; }
      return false;
    });
    return () => sub.remove();
  }, []);

  return (
    <View style={s.root}>
      <StatusBar barStyle="light-content" backgroundColor={BG} />
      {failed ? (
        <View style={s.center}>
          <Text style={s.t}>Não foi possível abrir o Onça</Text>
          <Text style={s.d}>{failed}</Text>
          <Pressable style={s.btn} onPress={() => { setFailed(null); web.current?.reload(); }}>
            <Text style={s.btnT}>Tentar de novo</Text>
          </Pressable>
        </View>
      ) : null}
      <WebView
        ref={web}
        style={[s.web, failed ? s.hidden : null]}
        source={{ uri: `${ORIGIN}/exec` }}
        injectedJavaScriptBeforeContentLoaded={injectedFlag(Platform.OS)}
        onMessage={onMessage}
        onShouldStartLoadWithRequest={onShouldStart}
        onOpenWindow={(e) => { const u = e.nativeEvent.targetUrl; if (u && !isPurchaseUrl(u)) Linking.openURL(u).catch(() => {}); }}
        onNavigationStateChange={(n) => { canGoBack.current = n.canGoBack; }}
        onError={(e) => setFailed(e.nativeEvent.description || "Sem conexão.")}
        javaScriptEnabled
        domStorageEnabled
        sharedCookiesEnabled
        limitsNavigationsToAppBoundDomains
        allowsBackForwardNavigationGestures
        pullToRefreshEnabled
        setSupportMultipleWindows
        originWhitelist={["https://*", "about:*"]}
        applicationNameForUserAgent="OncaApp/0.1"
      />
      {locked || covered ? (
        <View style={[StyleSheet.absoluteFill, s.cover]}>
          <Text style={s.brand}>ONÇA</Text>
          {locked ? (
            <Pressable style={s.btn} onPress={unlock}>
              <Text style={s.btnT}>Desbloquear</Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: BG, paddingTop: Platform.OS === "android" ? StatusBar.currentHeight : 44 },
  web: { flex: 1, backgroundColor: BG },
  hidden: { display: "none" },
  center: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24 },
  cover: { backgroundColor: BG, alignItems: "center", justifyContent: "center", gap: 24 },
  brand: { color: "#e8edf0", fontSize: 28, fontWeight: "700", letterSpacing: 4 },
  t: { color: "#e8edf0", fontSize: 18, fontWeight: "600", marginBottom: 8 },
  d: { color: "#9aa5ad", fontSize: 14, textAlign: "center", marginBottom: 20 },
  btn: { backgroundColor: "#3ec39a", borderRadius: 8, paddingHorizontal: 20, minHeight: 44, justifyContent: "center" },
  btnT: { color: "#06231b", fontWeight: "600", fontSize: 15 },
});
