"use client";

import { useEffect, useState } from "react";
import { authEnabled, completeSignIn } from "@/lib/auth";

/** OIDC redirect target: exchanges the authorization code (PKCE) and returns to the app. */
export default function AuthCallback() {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!authEnabled) {
      window.location.replace("/");
      return;
    }
    completeSignIn()
      .then(() => window.location.replace("/"))
      .catch(() => setError("Sign-in failed. Please try again."));
  }, []);

  return (
    <main className="grid min-h-screen place-items-center text-slate-300">
      {error ? (
        <div className="text-center">
          <p>{error}</p>
          <a href="/" className="mt-3 inline-block text-sky-300 underline">
            Back to Voyagent
          </a>
        </div>
      ) : (
        <p>Signing you in…</p>
      )}
    </main>
  );
}
