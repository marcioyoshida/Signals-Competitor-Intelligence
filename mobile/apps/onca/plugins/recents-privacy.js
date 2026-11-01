// #257 (ADR 029): the Android app switcher never shows /exec, lock on or off. Android 13+ (API 33)
// has Activity.setRecentsScreenshotEnabled(false): recents shows a blank card and screenshots still
// work. Older Android only has FLAG_SECURE, which also blocks screenshots, so there the shell sets it
// only while the app lock is on (App.tsx). iOS is covered in JS (the cover on AppState "inactive").
const MARK = "#257 recents privacy";

/** Pure: the MainActivity source with the call added after super.onCreate (idempotent). */
function addRecentsPrivacy(src, language) {
  if (src.includes(MARK)) return src;
  const kotlin = language !== "java";
  const call = kotlin
    ? `\n    // ${MARK}: no /exec content in the app switcher (API 33+; older: FLAG_SECURE while locked)\n    if (android.os.Build.VERSION.SDK_INT >= 33) setRecentsScreenshotEnabled(false)`
    : `\n    // ${MARK}: no /exec content in the app switcher (API 33+; older: FLAG_SECURE while locked)\n    if (android.os.Build.VERSION.SDK_INT >= 33) setRecentsScreenshotEnabled(false);`;
  const re = kotlin ? /super\.onCreate\([^)]*\)/ : /super\.onCreate\([^)]*\);/;
  if (!re.test(src)) throw new Error("recents-privacy: MainActivity has no super.onCreate(...) to anchor on");
  return src.replace(re, (m) => m + call);
}

function withRecentsPrivacy(config) {
  const { withMainActivity } = require("expo/config-plugins");   // lazy: the unit test needs no Expo
  return withMainActivity(config, (c) => {
    c.modResults.contents = addRecentsPrivacy(c.modResults.contents, c.modResults.language);
    return c;
  });
}

module.exports = withRecentsPrivacy;
module.exports.addRecentsPrivacy = addRecentsPrivacy;
