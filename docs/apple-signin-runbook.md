# Sign in with Apple — runbook (#248, ADR 029)

Onça's Cognito pool (`us-east-1_LfGQGWq56`, Hosted UI `onca-668449743071.auth.us-east-1.amazoncognito.com`)
gets a **SignInWithApple** identity provider next to Google. It is used on the web (Hosted UI button)
and by the store app (the same Hosted UI, opened in the system browser).

The CDK side is in `infra/app.py` (`OncaAppleIdp`). It is created **only** when all three IDs below are
in `infra/cdk.json` context. Never pass them as `-c` flags alone: an unrelated deploy without the
flags would silently destroy the IdP. That is what happened with Google on 2026-09-15.

## 1. Apple Developer portal (owner — needs the Apple ID + 2FA)

Team **AWSP6DBQ68**. https://developer.apple.com/account/resources/identifiers

**a) App ID**: Identifiers → **+** → *App IDs* → *App*.
- Description: `Onça`.
- Bundle ID (Explicit): `org.onssa.onca`.
- Capabilities:
  - **Sign in with Apple**, set as the *primary App ID*;
  - **Push Notifications** (for #256);
  - **Associated Domains** (for universal links, #255).
- Don't group it under Bluefin's App ID. Grouped apps share the user identifier and consent branding.

**b) Services ID** (this is the web "client ID"): Identifiers → **+** → *Services IDs*.
- Description: `Onça — entrar com a Apple`. This text appears on Apple's consent sheet.
- Identifier: `org.onssa.signin`.
- Tick **Sign in with Apple** → *Configure*:
  - Primary App ID: `org.onssa.onca`.
  - Domains and Subdomains: `onca-668449743071.auth.us-east-1.amazoncognito.com`.
  - Return URLs: `https://onca-668449743071.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`.
- Save, then Continue and Register.

**c) Key**: Keys → **+**.
- Name: `Onca Sign in with Apple`.
- Tick **Sign in with Apple** → *Configure* → Primary App ID `org.onssa.onca`.
- Register, then **Download** the `AuthKey_XXXXXXXXXX.p8`. Apple lets you download it **once**. Note
  the **Key ID** (10 characters).

**d) Email relay** (for #250, can be done now): Services → *Sign in with Apple for Email
Communication*. Add the Onça sending domain and From addresses so digests reach `@privaterelay` users.

## 2. Store the private key (owner — the key never goes in the repo or a chat)

```bash
aws secretsmanager create-secret --profile my2027 --region us-east-1 \
  --name signalscompetitor/onca/apple-oauth \
  --secret-string "$(jq -n --rawfile k ~/Downloads/AuthKey_XXXXXXXXXX.p8 '{private_key:$k}')"
```

Then delete the `.p8` from Downloads, or keep it in your password manager. Apple won't let you
download it again. If it is lost, revoke the key and create a new one.

## 3. Wire it (Claude)

Send Claude the **Key ID** only. The Services ID (`org.onssa.signin`) and team ID (`AWSP6DBQ68`) are
already known. Then Claude:
1. adds `apple_services_id`, `apple_team_id` and `apple_key_id` to `infra/cdk.json` context (not
   secrets);
2. runs the CDK diff, which should show only the new `AWS::Cognito::UserPoolIdentityProvider`
   (SignInWithApple) plus `SupportedIdentityProviders` gaining `SignInWithApple` on
   `OncaWarroomClient` and `OncaOAuthFacadeClient`, as in-place updates;
3. deploys;
4. verifies with
   `aws cognito-idp list-identity-providers --user-pool-id us-east-1_LfGQGWq56`;
5. signs in once with a real Apple ID on `/exec` (and on a `/v2` screen).

## 4. Behaviour to expect

- A first Apple login that maps to no tenant lands on the same gate as Google. That gate now names
  Apple and says the form creates a **new individual** Entry account.
- If the user hid their email (`…@privaterelay.appleid.com`), the gate tells them to sign out and
  sign in again choosing **Compartilhar meu e-mail**. A relay address can't match the corporate email
  an operator mapped. #249 adds invite codes so this works even with a hidden email.
- Apple sends the name only on the **first** authorization. Onça doesn't rely on it.
- The key doesn't expire, but rotate it if the team changes from individual to organization (#253).
  Moving App IDs between teams changes the user identifiers. Decide before many users sign in with
  Apple.
