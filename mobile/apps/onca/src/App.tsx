// ADR 029 (#254 spike → #255): the Onça store app is the /exec PWA in a WebView. The only native
// jobs here: sign-in in the SYSTEM browser (Google refuses embedded WebViews), external links out
// to the system browser, and Android back. Everything else is the web app.
import { useCallback, useEffect, useRef, useState } from "react";
import { BackHandler, Linking, Platform, Pressable, Share, StatusBar, StyleSheet, Text, View } from "react-native";
import { WebView } from "react-native-webview";
import type { WebViewMessageEvent } from "react-native-webview";
import type { ShouldStartLoadRequest } from "react-native-webview/lib/WebViewTypes";
import * as WebBrowser from "expo-web-browser";
import { ORIGIN, REDIRECT, exchangeUrl, injectedFlag, isPurchaseUrl, parseAuthRedirect, parseMessage, staysInApp } from "./bridge.ts";

const BG = "#0e1214";

export default function App() {
  const web = useRef<WebView>(null);
  const canGoBack = useRef(false);
  const [failed, setFailed] = useState<string | null>(null);

  // navigate INSIDE the same WebView (keeps sessionStorage, where /exec left the PKCE verifier)
  const go = useCallback((url: string) => {
    web.current?.injectJavaScript(`location.replace(${JSON.stringify(url)}); true;`);
  }, []);

  const onMessage = useCallback(async (e: WebViewMessageEvent) => {
    const m = parseMessage(e.nativeEvent.data);
    if (!m) return;
    if (m.type === "share") { Share.share({ message: m.url, url: m.url }).catch(() => {}); return; }
    const r = await WebBrowser.openAuthSessionAsync(m.url, REDIRECT, { preferEphemeralSession: m.type === "logout" });
    if (m.type === "logout") { go(`${ORIGIN}/exec`); return; }
    if (r.type !== "success") return;                    // cancelled: stay on the gate
    const res = parseAuthRedirect(r.url);
    if (res.kind === "code") go(exchangeUrl(res.code));
  }, [go]);

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
        limitsNavigationsToAppBoundDomains   // iOS: WKAppBoundDomains → service worker (offline) runs
        allowsBackForwardNavigationGestures
        pullToRefreshEnabled
        setSupportMultipleWindows
        originWhitelist={["https://*", "about:*"]}
        applicationNameForUserAgent="OncaApp/0.1"
      />
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: BG, paddingTop: Platform.OS === "android" ? StatusBar.currentHeight : 44 },
  web: { flex: 1, backgroundColor: BG },
  hidden: { display: "none" },
  center: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24 },
  t: { color: "#e8edf0", fontSize: 18, fontWeight: "600", marginBottom: 8 },
  d: { color: "#9aa5ad", fontSize: 14, textAlign: "center", marginBottom: 20 },
  btn: { backgroundColor: "#3ec39a", borderRadius: 8, paddingHorizontal: 20, minHeight: 44, justifyContent: "center" },
  btnT: { color: "#06231b", fontWeight: "600", fontSize: 15 },
});
