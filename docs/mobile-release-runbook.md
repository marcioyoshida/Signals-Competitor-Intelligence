# Onça app: release and incident runbook (#319, milestone Mobile M5)

Design: ADR 029. Monitoring: ADR 030. App code: `mobile/apps/onca`. Store text and assets:
`mobile/apps/onca/store/`. Most of the app is the web app (`/exec`), which ships with every site
deploy; this runbook covers the **native shell**.

## 1. What kind of change is it?

| Change | How it ships | Store review? |
|---|---|---|
| `/exec`, `context.js`, `app-guard.js`, any server code | Normal site/stack deploy | No |
| Shell JavaScript only (`src/*.ts(x)`), same native modules | **OTA update** (`mobile-release` workflow, kind `update`) | No |
| New or upgraded native module, `app.json` permissions/plugins, Expo SDK | **New build** (kind `build`), then store review | Yes |

`runtimeVersion` follows the app version (`policy: appVersion`). Any change in the third bucket
needs a **version bump** in `app.json` and `package.json`, or OTA updates would reach a binary that
can't run them.

## 2. Release (new build)

1. Bump `version` in `app.json` and `package.json`. Add a `CHANGELOG.md` entry.
2. `mobile-qa` must be green on `main`.
3. GitHub → Actions → **mobile-release** → `kind=build`, `platform`, `profile=production`,
   `submit=true`. The workflow re-runs mobile-qa, then the **quota guard** (refuses a build that
   would leave fewer than 2 builds per platform for the other apps on the Expo account).
4. **iOS:** the build lands in TestFlight. Internal testers get it at once. External testers need
   Beta App Review (about 1 day) the first time per version.
5. **Android:** the build lands on the Play **internal** track. Promote internal → closed →
   production in the Play Console.
6. Device pass (`docs/mobile-device-checklist.md`) on at least one iPhone and one Android phone.
7. Submit for review (App Store) or promote (Play) **with phased rollout**:
   - iOS: *Phased release for automatic updates*, 7 days.
   - Play: staged rollout 10% → 50% (after 2 days) → 100% (after 4 days).
8. Watch for the first week (section 4). Raise the minimum version only after section 5.

## 3. OTA update (shell JavaScript only)

Actions → **mobile-release** → `kind=update`, `profile` = the channel (`preview` first, then
`production`), `message` = what changed. Installed apps pick it up on their **next launch**.
Rollback: `npx eas-cli update:republish --group <previous group id>` on the same channel.

## 4. Go / no-go signals

| Signal | Where | Stop the rollout when |
|---|---|---|
| Crash-free launches | `Onca-AppHealth` alarm; Logs Insights on the session Lambda (`filter Kind = "fatal" | stats count() by version`) | under 99% on the new version |
| API errors from the app | same, `Kind = "error"` by `route`, `status` | a new spike on the new version |
| Sign-in | `scripts/auth_smoke.py`; canary checks `pretoken`, `session` | any failure |
| Store reviews | App Store Connect / Play Console, checked weekly | a repeated complaint about the new version |

**Pause:** App Store Connect → *Pause phased release*. Play Console → *Halt rollout*.
**Roll back:** OTA republish (section 3) if the bug is in shell JS; otherwise a fixed build with
an **expedited review** request (App Store Connect → Contact Us → *Request an expedited review*).
Web-side bugs are fixed with a normal deploy: no store involved.

## 5. Minimum supported version (#318)

`infra/cdk.json` → `app_min_versions` = `{"ios": {"soft": "x.y.z", "hard": "x.y.z"}, "android": …}`,
then deploy. Below `soft`: a dismissible "Há uma nova versão" banner (once a day). Below `hard`: a
full-screen "Atualize o app" with the store link and *Sair*. Shells older than bridge 4 report no
version and are never hard-blocked.

- Raise `soft` when a new version has been 100% rolled out for 7 days.
- Raise `hard` only for a security fix, or when a server change breaks old shells, and only
  after the new version has been available for 14 days.

## 6. Store review replies (templates)

- **2.1 (can't sign in):** "Please sign in with the demo account in *App Review Information*
  (Sign in → E-mail e senha). The account belongs to a reviewer-only organization with live
  content." Check first: canary `feed:app-store-review` green, and the password in SSM matches
  what's in App Store Connect.
- **4.2 (minimum functionality):** list the native features: Face ID / fingerprint app lock,
  content-free push alerts, offline reading of the last dashboard, native share sheet, universal
  links into a card, in-app account deletion.
- **4.8 (sign-in):** Sign in with Apple is offered next to Google on the sign-in sheet.
- **5.1.1(v) (deletion):** *Sua conta → Excluir minha conta*, immediate; also
  https://onssa.org/docs/excluir-conta.html.
- **3.1.1 (purchases):** the app has no purchase flow. Plans are bought by organizations on the
  website; in the app, purchase links are removed and the store URL is blocked.

## 7. Store reviewer account (#313)

The tenant `app-store-review` (SaaS: banking + fintech) exists and is excluded from all usage
metrics. The canary checks its feed every 4 hours. **Only the owner creates its login and sets the
password** (never in the repo, an issue or a chat):

```bash
aws cognito-idp admin-create-user --region us-east-1 --user-pool-id us-east-1_rxEkYhYHg --username app-review@onssa.org --user-attributes Name=email,Value=app-review@onssa.org Name=email_verified,Value=true Name=custom:tenant,Value=app-store-review Name=custom:tier,Value=saas --message-action SUPPRESS
```

```bash
read -rsp "Reviewer password (12+): " PW && aws cognito-idp admin-set-user-password --region us-east-1 --user-pool-id us-east-1_rxEkYhYHg --username app-review@onssa.org --password "$PW" --permanent && aws ssm put-parameter --region us-east-1 --name /onca/store-reviewer-password --type SecureString --overwrite --value "$PW" >/dev/null; unset PW
```

Then paste it into App Store Connect (*App Review Information*) and Play Console (*App access*).
**Rotate it after each review cycle** (re-run the second command, update both consoles).

## 8. Support

- Support address: contato@onssa.org (listed in the stores). Target: first answer within 2
  business days.
- In-app: *Reportar* on any answer or card (`OncaFeedback`, 90 days).
- Store reviews: read weekly; reply in the review's language, never with account details;
  route defects to a GitHub issue (no client data in public issues).

## 9. Accounts, keys and where they live (names only)

| Credential | Where | Rotation |
|---|---|---|
| Expo access token for CI | GitHub repo secret `EXPO_TOKEN` | yearly, or when someone leaves |
| App Store Connect API key (EAS submit) | EAS credentials (Expo account) | yearly |
| iOS distribution certificate + profile | EAS credentials | on expiry (EAS renews) |
| Android upload key | EAS credentials | never (Play App Signing holds the app key) |
| FCM V1 service-account key | EAS credentials | yearly |
| APNs key | EAS credentials | on revoke only |
| Reviewer password | SSM `/onca/store-reviewer-password` | after each review cycle |
| QA persona (CI Maestro) | GitHub secrets `QA_EMAIL` / `QA_PASSWORD` | yearly |

## 10. Play testing tracks (#316)

Internal (up to 100 testers, no review) → closed (named testers) → production. A **personal**
Play developer account must run a closed test with at least 12 testers for 14 days before
production access. The **organization** account (#253) is exempt, so the plan is to publish only
after #253.
