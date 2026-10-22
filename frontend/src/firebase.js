import { initializeApp, getApps } from 'firebase/app';
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signOut,
  onAuthStateChanged,
} from 'firebase/auth';

const runtimeEnv = (typeof window !== 'undefined' && window.__ENV__) || {};

const pickEnv = (runtimeKey, viteKey, fallback = '') => {
  const fromRuntime = runtimeEnv[runtimeKey];
  if (typeof fromRuntime === 'string' && fromRuntime.trim() !== '') {
    return fromRuntime.trim();
  }
  const fromVite = import.meta.env[viteKey];
  if (typeof fromVite === 'string' && fromVite.trim() !== '') {
    return fromVite.trim();
  }
  return fallback;
};

export const API_BASE = pickEnv(
  'INGESTION_API_URL',
  'VITE_INGESTION_API_URL',
  'http://localhost:8000'
).replace(/\/+$/, '');

export const AGENT_API_BASE = pickEnv(
  'AGENT_API_URL',
  'VITE_AGENT_API_URL',
  'http://localhost:8001'
).replace(/\/+$/, '');

export const firebaseConfig = {
  apiKey: pickEnv('FIREBASE_API_KEY', 'VITE_FIREBASE_API_KEY', ''),
  authDomain: pickEnv(
    'FIREBASE_AUTH_DOMAIN',
    'VITE_FIREBASE_AUTH_DOMAIN',
    'interactive-rag.firebaseapp.com'
  ),
  projectId: pickEnv('FIREBASE_PROJECT_ID', 'VITE_FIREBASE_PROJECT_ID', 'interactive-rag'),
  appId: pickEnv('FIREBASE_APP_ID', 'VITE_FIREBASE_APP_ID', ''),
};

export const isFirebaseConfigured = Boolean(firebaseConfig.apiKey);

export const isLocalhost =
  typeof window !== 'undefined' &&
  (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1');

let authInstance = null;
if (isFirebaseConfigured) {
  const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
  authInstance = getAuth(app);
}

export const auth = authInstance;

export async function signInWithGoogle() {
  if (!auth) {
    throw new Error('Firebase API Key is not configured yet.');
  }
  const provider = new GoogleAuthProvider();
  // Always show account chooser so users can switch Google accounts cleanly
  provider.setCustomParameters({ prompt: 'select_account' });
  const credential = await signInWithPopup(auth, provider);
  return credential.user;
}

export async function signInWithEmail(email, password) {
  if (!auth) {
    throw new Error('Firebase API Key is not configured yet.');
  }
  const credential = await signInWithEmailAndPassword(auth, email, password);
  return credential.user;
}

export async function signUpWithEmail(email, password) {
  if (!auth) {
    throw new Error('Firebase API Key is not configured yet.');
  }
  const credential = await createUserWithEmailAndPassword(auth, email, password);
  return credential.user;
}

export async function logoutUser() {
  if (auth) {
    await signOut(auth);
  }
}

export function subscribeToAuthChanges(callback) {
  if (!auth) {
    callback(null);
    return () => {};
  }
  return onAuthStateChanged(auth, callback);
}

export function formatAuthError(err) {
  const code = err?.code || '';
  switch (code) {
    case 'auth/popup-closed-by-user':
      return 'Google Sign-In popup was closed before completing sign-in.';
    case 'auth/popup-blocked':
      return 'Sign-in popup was blocked by your browser. Please allow popups for this site.';
    case 'auth/unauthorized-domain':
      return `This domain (${window.location.hostname}) is not yet listed in Identity Platform Authorized Domains.`;
    case 'auth/operation-not-allowed':
      return 'This sign-in provider is not enabled yet in Google Cloud Identity Platform.';
    case 'auth/invalid-credential':
    case 'auth/wrong-password':
    case 'auth/user-not-found':
      return 'Invalid email or password.';
    case 'auth/email-already-in-use':
      return 'An account with this email already exists. Try signing in instead.';
    case 'auth/weak-password':
      return 'Password must be at least 6 characters long.';
    default:
      return err?.message || 'Authentication failed. Please try again.';
  }
}
