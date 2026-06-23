import { useState, type FormEvent } from "react";
import { useLocation } from "wouter";
import LogoMark from "@/components/LogoMark";
import "./login.css";

export default function Login() {
  const [, setLocation] = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setLocation("/dashboard");
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
          <div className="form-sub">
            New here? <a href="#">Start your 14-day free trial</a>
          </div>
          <div className="field">
            <label htmlFor="email">Email Address</label>
            <input
              id="email"
              type="email"
              placeholder="you@company.com"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
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
          <a className="forgot" href="#">
            Forgot password?
          </a>
          <button className="submit-btn" type="submit">
            Sign In
          </button>
          <div className="divider">
            <div className="divider-line" />
            <div className="divider-text">or continue with</div>
            <div className="divider-line" />
          </div>
          <a className="social-btn" href="#">
            <svg
              width="18"
              height="18"
              viewBox="0 0 48 48"
              aria-hidden="true"
              focusable="false"
            >
              <path
                fill="#EA4335"
                d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
              />
              <path
                fill="#4285F4"
                d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
              />
              <path
                fill="#FBBC05"
                d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
              />
              <path
                fill="#34A853"
                d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
              />
            </svg>
            Continue with Google
          </a>
          <div className="form-footer">
            By signing in you agree to our <a href="#">Terms of Service</a> and{" "}
            <a href="#">Privacy Policy</a>
          </div>
        </form>
      </div>
    </main>
  );
}
