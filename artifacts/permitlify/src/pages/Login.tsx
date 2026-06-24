import { useState, type FormEvent } from "react";
import { useLocation } from "wouter";
import { useLogin } from "@workspace/api-client-react";
import LogoMark from "@/components/LogoMark";
import "./login.css";

export default function Login() {
  const [, setLocation] = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const login = useLogin();

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await login.mutateAsync({ data: { username, password } });
      setLocation("/dashboard");
    } catch {
      setError("Invalid username or password.");
    }
  }

  return (
    <main className="login-page">
      {/* LEFT — sales panel */}
      <div className="left">
        <div>
          <div className="logo">
            <LogoMark height={38} />
            <span className="logo-name">
              <span style={{ color: "#f1f5f9" }}>Match</span>
              <span style={{ color: "#49C85B" }}>Miner</span>
            </span>
          </div>
          <div className="sales">
            <div className="eyebrow">Tennis Intelligence Platform</div>
            <h2 className="headline">
              Fresh leads,
              <br />
              <span>every single day</span>
            </h2>
            <div className="subline">
              Get daily building permit alerts scored by AI — so you know exactly
              who to call before your competition does.
            </div>
            <div className="proof-row">
              <div className="proof-item">
                <div className="proof-ico a">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="var(--pr2)"
                    strokeWidth="2"
                    strokeLinecap="round"
                    aria-hidden="true"
                    focusable="false"
                  >
                    <circle cx="12" cy="12" r="3" />
                    <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4" />
                  </svg>
                </div>
                <div className="proof-text">
                  <div className="ptitle">Daily 6 AM Delivery</div>
                  <div className="psub">
                    New permits in your dashboard before the workday starts
                  </div>
                </div>
              </div>
              <div className="proof-item">
                <div className="proof-ico b">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="var(--ac2)"
                    strokeWidth="2"
                    strokeLinecap="round"
                    aria-hidden="true"
                    focusable="false"
                  >
                    <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
                  </svg>
                </div>
                <div className="proof-text">
                  <div className="ptitle">AI Lead Scoring</div>
                  <div className="psub">
                    Every permit ranked 0–100 so you call the best leads first
                  </div>
                </div>
              </div>
              <div className="proof-item">
                <div className="proof-ico c">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="var(--pr2)"
                    strokeWidth="2"
                    strokeLinecap="round"
                    aria-hidden="true"
                    focusable="false"
                  >
                    <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
                    <polyline points="22,6 12,13 2,6" />
                  </svg>
                </div>
                <div className="proof-text">
                  <div className="ptitle">CRM Integration</div>
                  <div className="psub">
                    Push to GoHighLevel, HubSpot, Zapier or any webhook
                  </div>
                </div>
              </div>
            </div>
            <div className="trust-row">
              <div className="trust-item">
                <div className="trust-dot" />
                No contracts
              </div>
              <div className="trust-item">
                <div className="trust-dot" />
                Cancel anytime
              </div>
              <div className="trust-item">
                <div className="trust-dot" />
                14-day free trial
              </div>
            </div>
          </div>
        </div>
        <div className="copyright">© 2026 MatchMiner.com</div>
      </div>

      {/* RIGHT — sign in form */}
      <div className="right">
        <form className="form-box" onSubmit={handleSubmit}>
          <div className="mob-logo">
            <LogoMark height={36} />
            <span className="mob-name">
              <span style={{ color: "#1C2F74" }}>Match</span>
              <span style={{ color: "#49C85B" }}>Miner</span>
            </span>
          </div>
          <h1 className="form-title">Welcome back</h1>
          <div className="field">
            <label htmlFor="username">Username</label>
            <input
              id="username"
              type="text"
              placeholder="Your username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              placeholder="••••••••"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          <button
            className="submit-btn"
            type="submit"
            disabled={login.isPending}
          >
            {login.isPending ? "Signing in…" : "Sign In"}
          </button>
        </form>
      </div>
    </main>
  );
}
