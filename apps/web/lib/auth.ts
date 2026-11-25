/**
 * Sign-in with the Cognito Hosted UI (OIDC authorization code + PKCE).
 *
 * NEXT_PUBLIC_AUTH_MODE=disabled (default, local dev): no sign-in; the API uses a fixed local user.
 * NEXT_PUBLIC_AUTH_MODE=cognito: needs NEXT_PUBLIC_COGNITO_ISSUER, NEXT_PUBLIC_COGNITO_CLIENT_ID,
 * NEXT_PUBLIC_COGNITO_DOMAIN (Hosted UI domain, for logout). Public values only: the app client
 * has no secret.
 *
 * Tokens live in sessionStorage (cleared when the tab closes) and are sent as a bearer token to
 * the API. A server-side session (BFF with httpOnly cookies) would keep them away from page
 * scripts entirely; tracked in docs/SECURITY.md.
 */
import { UserManager, WebStorageStateStore, type User } from "oidc-client-ts";

export const AUTH_MODE = (process.env.NEXT_PUBLIC_AUTH_MODE || "disabled").toLowerCase();
export const authEnabled = AUTH_MODE === "cognito";

let manager: UserManager | null = null;

function getManager(): UserManager {
  if (!authEnabled) throw new Error("Auth is disabled");
  if (!manager) {
    const origin = window.location.origin;
    manager = new UserManager({
      authority: process.env.NEXT_PUBLIC_COGNITO_ISSUER || "",
      client_id: process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "",
      redirect_uri: `${origin}/auth/callback`,
      post_logout_redirect_uri: origin,
      response_type: "code",
      scope: "openid email",
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: false,
    });
  }
  return manager;
}

export async function currentUser(): Promise<User | null> {
  if (!authEnabled) return null;
  const user = await getManager().getUser();
  return user && !user.expired ? user : null;
}

/** Bearer token for API calls, or null (signed out / auth disabled). */
export async function accessToken(): Promise<string | null> {
  return (await currentUser())?.access_token ?? null;
}

export async function signIn(): Promise<void> {
  await getManager().signinRedirect();
}

export async function completeSignIn(): Promise<void> {
  await getManager().signinRedirectCallback();
}

export async function signOut(): Promise<void> {
  await getManager().removeUser();
  // Cognito has no OIDC end_session_endpoint; use the Hosted UI logout URL.
  const domain = (process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "").replace(/\/$/, "");
  const clientId = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "";
  if (domain) {
    const params = new URLSearchParams({ client_id: clientId, logout_uri: window.location.origin });
    window.location.assign(`${domain}/logout?${params}`);
  } else {
    window.location.reload();
  }
}
