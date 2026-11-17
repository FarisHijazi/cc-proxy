# Self-hosted T3 authentication

The five-patch [T3 series](../patches/t3/README.md) adds Authentik sign-in to the existing
T3 environment. Open its HTTPS address in a desktop browser or iPhone Safari and
sign in with an allowed identity. Safari's **Add to Home Screen** uses the same
served application; no custom iPhone app is required. Devices see the same
server-side projects and fleet conversations and renew their own sessions.
Completing sign-in or renewal recovers the existing browser connection without
requiring a manual reload.
There are no user-facing pairing codes, QR links, Clerk account or T3 Connect relay
in this sign-in path.

Matching custom desktop/mobile builds also support this flow through **Add
Environment** or **Sign in to your server**. The stock apps do not acquire OIDC
support from a server upgrade: their direct-connection credentials lack this
renewal flow, and production mobile does not accept automatic pairing deep-link
prefill. Use the served web application when a custom native build is unwanted.
Native source support is not proof that a signed app has been built or installed.

## Authentik provider

Create a dedicated confidential OAuth2/OIDC provider and bind its application to
the intended users. Use authorization code with S256 PKCE, RS256 signing,
`openid profile offline_access` scopes, a 15-minute access lifetime and a 30-day
rotating refresh lifetime. The provider must return a signed ID token on refresh;
the integration validates its issuer, audience and subject without a second
userinfo request after rotating the upstream credential. Authentik's tested
refresh implementation supplies that token.

Register exactly this provider callback, substituting the public T3 origin:

```text
https://t3.example.com/api/auth/oidc/callback
```

This server callback differs from the client callbacks below. The confidential
client secret stays on the server. Use exact allowed `sub` claims, not email or
display-name matching. With Authentik's `hashed_user_id` subject mode, the tested
provider uses the user's `uid`; confirm the configured mode when provisioning.

## Server configuration

Store the JSON and client-secret file outside userdata and version control, owned
by the service account with mode `0600`. Example JSON:

```json
{
  "issuer": "https://auth.example.com/application/o/t3code/",
  "clientId": "your-confidential-client-id",
  "clientSecretFile": "/srv/t3-config/oidc-client-secret",
  "publicOrigin": "https://t3.example.com",
  "allowedSubjects": ["exact-owner-subject"],
  "redirectUris": ["https://t3.example.com/auth/oidc/callback"]
}
```

Keep the issuer's exact discovered value, including its trailing slash.
`publicOrigin` must be an HTTPS origin without a path. `redirectUris` is the
server's exact allowlist of client handoff destinations; add only clients used by
this deployment:

| Client | Exact callback |
| --- | --- |
| Served browser / Safari | `https://t3.example.com/auth/oidc/callback` |
| Custom desktop | `t3code://app/auth/oidc/callback` |
| Desktop development build | `t3code-dev://app/auth/oidc/callback` |
| Custom mobile | `t3code://auth/oidc/callback` |
| Mobile development build | `t3code-dev://auth/oidc/callback` |
| Mobile preview build | `t3code-preview://auth/oidc/callback` |

Upsert these keys in the existing `<t3-home>/service.env`, preserving its network
and other settings. T3's `cli/service.ts::writeServiceEnv` is the existing writer;
manual service-unit replacements are unnecessary.

```dotenv
T3CODE_OIDC_CONFIG_FILE=/srv/t3-config/oidc.json
T3CODE_OIDC_REQUIRED=true
T3CODE_PORT=3773
```

With `REQUIRED=true`, missing or invalid configuration fails startup. Keep both
OIDC settings together. Omitting configuration intentionally preserves upstream
behavior for installations that do not use this feature.

## Public routing

Reuse the existing tunnel, reverse proxy and T3 service. Keep the UI and static
assets behind Authentik forward authentication and disable public caching for the
T3 hostname. Native clients cannot satisfy an interactive forward-auth redirect
on every API request, so route only `/api/`, `/ws` and
`/.well-known/t3/environment` directly to T3's existing auth owner.

T3 validates protected API/WebSocket access against a current, persisted,
OIDC-derived device grant. Anonymous requests, invalid bearer tokens, legacy
remote pairing credentials and spoofed Authentik/forwarding headers do not grant
access. The descriptor and OIDC configuration are public metadata; authorization,
callback, token and logout endpoints enforce their own protocol checks. Keep the
backend listener private. Physical loopback CLI administration retains its
existing credentials; forwarded localhost claims do not qualify.

The packaged upstream CORS policy permits cross-origin bearer clients without
allowing browser credentials. Browser token and logout operations additionally
require the exact public Origin; missing, foreign and `null` Origins are rejected.
Browser refresh secrets remain in Secure, HttpOnly cookies. Native builds use the
existing platform credential stores. No proxy identity header substitutes for a
validated session.

## Build the macOS desktop client

Use a clean arm64 Mac packaging worktree at the maintained source in the
[patch README](../patches/t3/README.md), with its five patches already applied.
Follow T3's `docs/operations/development.md` for tooling. The verified build used
Node `26.13.1`, pnpm `12.14.0`, Vite+ `1.4.0`, Rust `1.98.1` and Xcode `27.2`;
the pinned T3 manifests remain the toolchain authority when updating. Select the
matching Node and `vp` on the build shell's `PATH`, then run `vp i --frozen-lockfile`.

Keep `.env` and `.env.local` absent in this packaging worktree: T3 reads them even
when the process environment is clean. The clean environment below omits all
hosted Clerk/relay configuration and both update-feed selectors,
`GITHUB_REPOSITORY` and `T3CODE_DESKTOP_UPDATE_REPOSITORY`. Substitute the actual
self-hosted HTTPS origin and use the release version from the patch README:

```sh
T3_DESKTOP_VERSION=0.1.10-ccfleet.20261117.3
env -i HOME="$HOME" USER="$USER" PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" \
  VITE_SELF_HOSTED_SERVER_URL=https://t3.example.com \
  vp run dist:desktop:artifact --platform mac --target dmg --arch arm64 \
  --build-version "$T3_DESKTOP_VERSION" --output-dir release/unsigned \
  --keep-stage --verbose
```

This upstream script builds web/server/desktop bundles, compiles the native
resource monitor, stages dependencies, prepares server bytecode without starting
services, and validates the macOS payload. Require its successful payload check.
It restores release manifests after compilation; review remaining generated
lockfile changes before keeping the worktree.

For a local Mac installation, seal the complete bundle with electron-builder's
supported ad-hoc signing options. Set `T3_DESKTOP_STAGE` to the generated `app`
directory printed in that build's verbose output, then run from the T3 worktree:

```sh
T3_DESKTOP_STAGE=/absolute/path/from/build-log/app
env -i HOME="$HOME" USER="$USER" PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" \
  CSC_IDENTITY_AUTO_DISCOVERY=false \
  vp exec --filter @t3tools/desktop -- electron-builder \
  --projectDir "$T3_DESKTOP_STAGE" --mac --arm64 --publish never \
  --config.mac.identity=- --config.mac.hardenedRuntime=false
```

Keep this second pass's default output directory, `$T3_DESKTOP_STAGE/dist`.
Overriding `directories.output` to another directory makes the previous `dist`
eligible for packaging and embeds old artifacts inside the app. Copy only the
finished app, DMG and ZIP out of `dist` after signing; `release/unsigned` is not
the install candidate. No new builder or source modification is needed.

```sh
T3_DESKTOP_APP="$T3_DESKTOP_STAGE/dist/mac-arm64/T3 Code (Beta).app"
codesign --verify --deep --strict "$T3_DESKTOP_APP"
codesign -dv "$T3_DESKTOP_APP"
file "$T3_DESKTOP_APP/Contents/MacOS/T3 Code (Beta)"
xattr "$T3_DESKTOP_APP"
hdiutil verify "$T3_DESKTOP_STAGE/dist/T3-Code-$T3_DESKTOP_VERSION-arm64.dmg"
unzip -t -q "$T3_DESKTOP_STAGE/dist/T3-Code-$T3_DESKTOP_VERSION-arm64.zip"
shasum -a 256 "$T3_DESKTOP_STAGE/dist/T3-Code-$T3_DESKTOP_VERSION-arm64.dmg" \
  "$T3_DESKTOP_STAGE/dist/T3-Code-$T3_DESKTOP_VERSION-arm64.zip"
```

Require the expected version/source in the packaged manifest, bundle ID
`com.t3tools.t3code`, arm64 binaries, sealed resources, the configured self-hosted
origin, no `app-update.yml`, and no top-level `dist` inside `app.asar`. This is a
local ad-hoc build, not a notarized distribution. Confirm the locally built app
has no quarantine attribute; do not remove quarantine or change system security.

Before replacing the installed app, confirm it owns no active agents or terminals,
quit only that app, and back up `~/.t3/userdata`, the existing
`~/Library/Application Support/t3code-v2` profile and old app. Preserve the Keychain
and install at the same app path. For a remote-only desktop, keep
`localEnvironmentEnabled=false` before launch. Verify sign-in, fleet history,
quit/reopen persistence, renewal and a remote send/reply through the real client;
record acceptance in the [fleet test matrix](testing/fleet-e2e-matrix.md).

## Renewal, logout and upgrades

Access sessions last at most 15 minutes; automatic refresh is bounded by the
30-day device grant. Expiry or revocation can require normal Authentik sign-in
again, without pairing codes. Provider rejection revokes the device grant;
temporary provider failures preserve it for retry. Logout revokes the local
grant/session, removes the stored provider credential and requests upstream
revocation. Provider revocation takes effect when Authentik rejects the next
renewal; it is not pushed instantly into an already issued access session.

The implementation extends the existing EnvironmentAuth, SessionStore,
AuthSessionRepository and ServerSecretStore owners. It does not create another
fleet/session database, credential writer, server or relay. Fleet discovery and
worker control remain with the existing ACP/HQ integration.

Install a verified custom archive through T3's official installer. Activate its
exact cached binary with `t3 service install --base-dir <existing-t3-home>` after
the backups described in the [patch README](../patches/t3/README.md). Preserve the
existing data directory and worker processes. T3's server does not update itself
automatically; a manual official update can overwrite this patch and will not
understand its OIDC settings. Rebase, rebuild and retest before upgrading. No
systemwide update policy is required by this setup.

Verify the packaged build in a fresh isolated home, then check public anonymous
API/WebSocket rejection and a real sign-in/refresh/logout in the intended browser.
Source tests and a successful service start do not replace browser acceptance;
current deployment evidence belongs in the
[fleet test matrix](testing/fleet-e2e-matrix.md).
