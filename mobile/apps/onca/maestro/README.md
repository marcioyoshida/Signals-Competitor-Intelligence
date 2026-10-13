# Maestro flows: native E2E of the store shell (#259, Layer 2)

The flows drive the real app (WebView of `https://onssa.org/exec`, sign-in in the system browser)
on an iOS Simulator and an Android Emulator. They run on EAS Workflows through
`.eas/workflows/e2e.yml`. Layer 1 (the same `/exec` in app mode, run from WSL in WebKit, nightly
and after each deploy) is `scripts/e2e_app.sh` at the repo root.

| Flow | Credentials | Checks |
|---|---|---|
| `launch.yaml` | none | gate shows Entrar, no "Assinar"/"R$", no tab bar |
| `auth_cancel.yaml` | none | backing out of sign-in returns to the gate WITHOUT "Login não concluído" |
| `login.yaml` | QA | sign-in → tab bar; offline relaunch (Android only); sign-out via "Sua conta" → "Sair desta conta" |
| `tabbar.yaml` | QA | Estratégia/Regulatório/Compliance/Produto each switch the Painel view |
| `subflows/sign_in.yaml` | QA | shared: gate → system-browser sign-in → tab bar |

## One-time setup (owner)

1. **Plan.** EAS rejects `maestro` jobs on the free plan: `eas workflow:validate` returns
   "Running maestro_test jobs requires a paid plan". Subscribe at
   https://expo.dev/accounts/marcioy/settings/billing. Each run also spends two builds (an iOS
   simulator build and an Android APK) plus macOS and Linux nested-virtualization minutes.
2. **QA credentials.** Use the `qa-test-admin@…` persona: it is the only one with the four
   officer tabs. Its password is in Secrets Manager `signalscompetitor/onca/qa-test-credentials`,
   under `personas.admin`. Maestro only reads environment variables named `MAESTRO_*`, so the
   variable names carry that prefix. The maestro jobs run in the `preview` environment. Leave out
   `--value` so the CLI prompts for it and the password stays out of shell history:

   ```sh
   cd mobile/apps/onca
   npx eas-cli@latest env:set preview --name MAESTRO_QA_EMAIL    --visibility sensitive
   npx eas-cli@latest env:set preview --name MAESTRO_QA_PASSWORD --visibility secret
   ```

## Run

```sh
cd mobile/apps/onca
npx eas-cli@latest workflow:validate .eas/workflows/e2e.yml   # schema + plan check, no run
npx eas-cli@latest workflow:run .eas/workflows/e2e.yml        # builds both apps, then runs the flows
```

Results, screenshots and screen recordings are in the run's "Maestro Test Results" artifact on
expo.dev. Locally, with a simulator or emulator that has the app installed:

```sh
MAESTRO_QA_EMAIL=… MAESTRO_QA_PASSWORD=… maestro test maestro/login.yaml
maestro check-syntax maestro/login.yaml
```

## Known expectations

- **`auth_cancel.yaml` fails on Android with the current shell.** expo-web-browser 57 reports a
  closed Custom Tab as `dismiss`, and `App.tsx` (`authFailureMessage`) shows
  "Login não concluído" for `dismiss`. On iOS, Cancel reports `cancel`, which stays quiet.
- `setAirplaneMode` only works on Android. On iOS, `login.yaml` skips the offline step. Layer 1
  covers the offline view.
- The iOS sign-in session is not ephemeral, so Safari can keep a Cognito session between flows.
  When it does, the sign-in subflow skips the form.
