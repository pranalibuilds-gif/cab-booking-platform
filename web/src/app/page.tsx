"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import RideDashboard from "./RideDashboard";

type User = {
  id: string;
  email: string;
  full_name: string;
  role: "rider" | "driver" | "admin";
};

type AuthResponse = {
  access_token: string;
  user: User;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
let bootstrapRequest: Promise<AuthResponse | null> | null = null;

async function readError(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as
    | { detail?: string | Array<{ msg?: string }> }
    | null;
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) return body.detail[0]?.msg ?? "Please check your details.";
  return "The request could not be completed. Check that the API is running and try again.";
}

async function refreshSession(): Promise<AuthResponse | null> {
  if (!bootstrapRequest) {
    bootstrapRequest = fetch(`${apiBase}/api/v1/auth/refresh`, {
      method: "POST",
      credentials: "include",
    })
      .then(async (response) => {
        if (response.status === 401) return null;
        if (!response.ok) throw new Error(await readError(response));
        return (await response.json()) as AuthResponse;
      })
      .finally(() => {
        bootstrapRequest = null;
      });
  }
  return bootstrapRequest;
}

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [role, setRole] = useState<"rider" | "driver">("rider");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    refreshSession()
      .then((session) => {
        if (active && session) {
          setAccessToken(session.access_token);
          setUser(session.user);
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to restore session.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      if (mode === "register") {
        const registration = await fetch(`${apiBase}/api/v1/auth/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ full_name: fullName, email, password, role }),
        });
        if (!registration.ok) throw new Error(await readError(registration));
      }

      const response = await fetch(`${apiBase}/api/v1/auth/login`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!response.ok) throw new Error(await readError(response));
      const session = (await response.json()) as AuthResponse;
      setAccessToken(session.access_token);
      setUser(session.user);
      setPassword("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to sign in.");
    } finally {
      setSubmitting(false);
    }
  }

  const handleLogout = useCallback(async () => {
    setError("");
    try {
      const response = await fetch(`${apiBase}/api/v1/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) throw new Error(await readError(response));
      setAccessToken(null);
      setUser(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to sign out.");
    }
  }, []);
  const handleSessionExpired = useCallback(() => {
    void handleLogout();
  }, [handleLogout]);

  return (
    <main className="shell">
      <header className="topbar">
        <Link className="brand" href="/" aria-label="Ridewell home">
          <span className="brand-mark">R</span>
          <span>ridewell</span>
        </Link>
        <span className="local-badge"><span /> Local MVP</span>
      </header>

      {user && accessToken ? (
        <section className="workspace">
          <RideDashboard
            user={user}
            accessToken={accessToken}
            onAccessToken={setAccessToken}
            onLogout={handleSessionExpired}
          />
        </section>
      ) : (
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow">YOUR CITY, YOUR RIDE</p>
            <h1>Getting there<br />should feel <em>easy.</em></h1>
            <p className="hero-description">
              A smoother way to move around. Sign in to get started, or create an
              account as a rider or driver.
            </p>
            <div className="trust-note">
              <span className="trust-icon">✳</span>
              <span>Thoughtful trips. People-first service.</span>
            </div>
          </div>

          <div className="auth-card">
            {loading ? (
              <div className="loading-state" role="status">Checking your session…</div>
            ) : (
              <>
              <div className="card-heading">
                <div>
                  <p className="eyebrow">{mode === "login" ? "WELCOME BACK" : "GOOD TO HAVE YOU"}</p>
                  <h2>{mode === "login" ? "Sign in" : "Create your account"}</h2>
                </div>
                <span className="card-spark">✳</span>
              </div>
              <div className="mode-switch" role="tablist" aria-label="Account action">
                <button
                  className={mode === "login" ? "mode-tab active" : "mode-tab"}
                  onClick={() => { setMode("login"); setError(""); }}
                  role="tab"
                  aria-selected={mode === "login"}
                  type="button"
                >
                  Sign in
                </button>
                <button
                  className={mode === "register" ? "mode-tab active" : "mode-tab"}
                  onClick={() => { setMode("register"); setError(""); }}
                  role="tab"
                  aria-selected={mode === "register"}
                  type="button"
                >
                  Create account
                </button>
              </div>
              <form className="auth-form" onSubmit={handleSubmit}>
                {mode === "register" && (
                  <>
                    <label htmlFor="full-name">Your name</label>
                    <input
                      id="full-name"
                      autoComplete="name"
                      maxLength={120}
                      onChange={(event) => setFullName(event.target.value)}
                      placeholder="e.g. Alex Morgan"
                      required
                      value={fullName}
                    />
                    <label htmlFor="role">I’m joining as a</label>
                    <select id="role" onChange={(event) => setRole(event.target.value as "rider" | "driver")} value={role}>
                      <option value="rider">Rider</option>
                      <option value="driver">Driver</option>
                    </select>
                  </>
                )}
                <label htmlFor="email">Email address</label>
                <input
                  id="email"
                  autoComplete="email"
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  required
                  type="email"
                  value={email}
                />
                <label htmlFor="password">Password</label>
                <input
                  id="password"
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  minLength={mode === "register" ? 12 : 1}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={mode === "login" ? "Enter your password" : "At least 12 characters"}
                  required
                  type="password"
                  value={password}
                />
                {error && <p className="form-error" role="alert">{error}</p>}
                <button className="button button-primary" disabled={submitting} type="submit">
                  {submitting ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
                  {!submitting && <span aria-hidden="true">→</span>}
                </button>
              </form>
              <p className="fine-print">
                By continuing, you agree to our terms and acknowledge our privacy policy.
              </p>
              </>
            )}
          </div>
        </section>
      )}
      <footer className="footer">
        <span>Made for the journeys that make a day.</span>
        <span>© 2026 Ridewell</span>
      </footer>
      {accessToken && <span className="sr-only" aria-live="polite">Authenticated</span>}
    </main>
  );
}
