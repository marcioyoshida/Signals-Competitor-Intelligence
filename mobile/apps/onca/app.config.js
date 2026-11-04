// Extends app.json. google-services.json (Firebase/FCM, #316) is NEVER committed —
// this repo is public. EAS injects it at build time from the secret file env var
// GOOGLE_SERVICES_JSON (preview/production/development). Locally it is unset, so
// the field is omitted and `expo config` still resolves.
module.exports = ({ config }) => {
  const googleServicesFile = process.env.GOOGLE_SERVICES_JSON;
  if (!googleServicesFile) return config;
  return { ...config, android: { ...config.android, googleServicesFile } };
};
