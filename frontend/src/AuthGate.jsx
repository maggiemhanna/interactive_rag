import React, { useState } from 'react';
import {
  Database,
  Mail,
  Lock,
  AlertCircle,
  ShieldCheck,
  Terminal,
  RefreshCw,
} from 'lucide-react';
import {
  isFirebaseConfigured,
  isLocalhost,
  signInWithGoogle,
  signInWithEmail,
  signUpWithEmail,
  formatAuthError,
} from './firebase';

function GoogleLogoIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
      <path
        fill="#EA4335"
        d="M12 10.2v3.9h5.4c-.2 1.2-.9 2.3-2 3l3.2 2.5c1.9-1.7 3-4.3 3-7.4 0-.7-.1-1.4-.2-2H12z"
      />
      <path
        fill="#34A853"
        d="M12 22c2.7 0 5-.9 6.6-2.4l-3.2-2.5c-.9.6-2 .9-3.4.9-2.6 0-4.8-1.8-5.6-4.1l-3.3 2.6C4.7 19.8 8.1 22 12 22z"
      />
      <path
        fill="#4A90E2"
        d="M6.4 13.9c-.2-.6-.3-1.2-.3-1.9s.1-1.3.3-1.9L3.1 7.5C2.4 8.9 2 10.4 2 12s.4 3.1 1.1 4.5l3.3-2.6z"
      />
      <path
        fill="#FBBC05"
        d="M12 5.9c1.5 0 2.8.5 3.8 1.5l2.9-2.9C17 2.9 14.7 2 12 2 8.1 2 4.7 4.2 3.1 7.5l3.3 2.6c.8-2.3 3-4.2 5.6-4.2z"
      />
    </svg>
  );
}

export default function AuthGate({ onLocalDevLogin }) {
  const [mode, setMode] = useState('signin'); // 'signin' | 'signup'
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [devUserId, setDevUserId] = useState('local-dev');
  const [googleBusy, setGoogleBusy] = useState(false);
  const [emailBusy, setEmailBusy] = useState(false);
  const [error, setError] = useState('');

  const handleGoogleClick = async () => {
    setError('');
    if (!isFirebaseConfigured) {
      setError(
        'Set VITE_FIREBASE_API_KEY in frontend/.env (or deploy via Terraform) to activate live Google OAuth.'
      );
      return;
    }
    setGoogleBusy(true);
    try {
      await signInWithGoogle();
    } catch (err) {
      setError(formatAuthError(err));
    } finally {
      setGoogleBusy(false);
    }
  };

  const handleEmailSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (!isFirebaseConfigured) {
      setError(
        'Set VITE_FIREBASE_API_KEY in frontend/.env (or deploy via Terraform) to activate Identity Platform login.'
      );
      return;
    }
    if (!email.trim() || !password) {
      setError('Please enter both your email address and password.');
      return;
    }

    setEmailBusy(true);
    try {
      if (mode === 'signup') {
        await signUpWithEmail(email.trim(), password);
      } else {
        await signInWithEmail(email.trim(), password);
      }
    } catch (err) {
      setError(formatAuthError(err));
    } finally {
      setEmailBusy(false);
    }
  };

  const handleDevSubmit = (e) => {
    e.preventDefault();
    const cleaned = devUserId.trim();
    if (!/^[a-zA-Z0-9_-]{1,128}$/.test(cleaned)) {
      setError('Local Dev User ID must contain only letters, numbers, hyphens, or underscores.');
      return;
    }
    onLocalDevLogin({
      uid: cleaned,
      email: `${cleaned}@local.dev`,
      displayName: `Local Dev (${cleaned})`,
      isLocalDev: true,
    });
  };

  return (
    <div className="auth-screen">
      <div className="glass-card auth-card">
        <div className="auth-header">
          <div className="logo-icon">
            <Database size={28} />
          </div>
          <div>
            <h1 className="brand-title">Interactive RAG Portal</h1>
            <p className="brand-subtitle">
              Sign in to manage &amp; query your private Chroma vector stores
            </p>
          </div>
        </div>

        <div className="auth-isolation-badge">
          <ShieldCheck size={16} color="var(--accent-emerald)" />
          <span>Per-user isolated vector persistence on Google Cloud Storage</span>
        </div>

        {error && (
          <div className="alert alert-error" style={{ marginBottom: '1.25rem' }}>
            <AlertCircle size={18} style={{ flexShrink: 0 }} />
            <span>{error}</span>
          </div>
        )}

        {/* Primary Google OAuth Sign-In Button */}
        <button
          type="button"
          className="google-auth-btn"
          onClick={handleGoogleClick}
          disabled={googleBusy || emailBusy}
        >
          {googleBusy ? (
            <>
              <RefreshCw size={18} className="spinner" />
              <span>Connecting to Google...</span>
            </>
          ) : (
            <>
              <GoogleLogoIcon />
              <span>Continue with Google</span>
            </>
          )}
        </button>

        <div className="auth-divider">
          <span>or continue with email</span>
        </div>

        <div className="tab-group" style={{ marginBottom: '1.25rem' }}>
          <button
            type="button"
            className={`tab-btn ${mode === 'signin' ? 'active' : ''}`}
            onClick={() => {
              setMode('signin');
              setError('');
            }}
          >
            Sign In
          </button>
          <button
            type="button"
            className={`tab-btn ${mode === 'signup' ? 'active' : ''}`}
            onClick={() => {
              setMode('signup');
              setError('');
            }}
          >
            Create Account
          </button>
        </div>

        <form onSubmit={handleEmailSubmit}>
          <div className="form-group" style={{ marginBottom: '1rem' }}>
            <label className="form-label">Email Address</label>
            <div className="auth-input-wrapper">
              <Mail size={16} className="auth-input-icon" />
              <input
                type="email"
                className="form-input auth-input-padded"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
              />
            </div>
          </div>

          <div className="form-group" style={{ marginBottom: '1.25rem' }}>
            <label className="form-label">Password</label>
            <div className="auth-input-wrapper">
              <Lock size={16} className="auth-input-icon" />
              <input
                type="password"
                className="form-input auth-input-padded"
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
              />
            </div>
          </div>

          <button
            type="submit"
            className="submit-btn"
            disabled={emailBusy || googleBusy}
            style={{ width: '100%' }}
          >
            {emailBusy ? (
              <>
                <RefreshCw size={18} className="spinner" />
                <span>{mode === 'signup' ? 'Creating Account...' : 'Signing In...'}</span>
              </>
            ) : (
              <span>{mode === 'signup' ? 'Create Account' : 'Sign In with Email'}</span>
            )}
          </button>
        </form>

        {/* Localhost-only helper when VITE_FIREBASE_API_KEY is not yet configured */}
        {!isFirebaseConfigured && isLocalhost && (
          <form onSubmit={handleDevSubmit} className="auth-dev-panel">
            <div className="auth-dev-title">
              <Terminal size={15} color="var(--accent-cyan)" />
              <span>Local Dev Mode (No VITE_FIREBASE_API_KEY detected)</span>
            </div>
            <p className="auth-dev-hint">
              To test live Google OAuth locally, add <code>VITE_FIREBASE_API_KEY</code> to{' '}
              <code>frontend/.env</code>. For offline multi-tenant testing with{' '}
              <code>AUTH_DISABLED=true</code>, enter a simulated tenant ID:
            </p>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <input
                type="text"
                className="form-input"
                value={devUserId}
                onChange={(e) => setDevUserId(e.target.value)}
                placeholder="e.g., alice or bob"
                style={{ padding: '0.55rem 0.85rem', fontSize: '0.85rem' }}
              />
              <button type="submit" className="tab-btn active" style={{ flex: '0 0 auto' }}>
                Enter Dev Mode
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
