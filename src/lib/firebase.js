/* Firebase is imported lazily so the auth UI can paint before the SDK arrives. */

// Prefer Vite env vars; fall back to project defaults for local demos.
const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || 'AIzaSyBdsEwt7eAmBjtGDk_ns68QSrTK77mpWkQ',
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || 'churn-prediction-model-ai.firebaseapp.com',
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || 'churn-prediction-model-ai',
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || 'churn-prediction-model-ai.firebasestorage.app',
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || '24302908104',
  appId: import.meta.env.VITE_FIREBASE_APP_ID || '1:24302908104:web:8a907a48ea41ccbbad6bfd',
  measurementId: import.meta.env.VITE_FIREBASE_MEASUREMENT_ID || 'G-SP7GXCV9J2',
};

let cached = null;

export function loadFirebase() {
  cached ??= (async () => {
    const [{ initializeApp }, authMod] = await Promise.all([
      import('firebase/app'),
      import('firebase/auth'),
    ]);
    const app = initializeApp(firebaseConfig);
    const auth = authMod.getAuth(app);

    // Analytics is non-essential: load it when the browser is idle.
    const idle = window.requestIdleCallback || (cb => setTimeout(cb, 2000));
    idle(() => {
      import('firebase/analytics')
        .then(({ getAnalytics, isSupported }) => isSupported().then(ok => ok && getAnalytics(app)))
        .catch(() => {});
    });

    return {
      auth,
      onAuthStateChanged: authMod.onAuthStateChanged,
      signInWithEmailAndPassword: authMod.signInWithEmailAndPassword,
      createUserWithEmailAndPassword: authMod.createUserWithEmailAndPassword,
      signInWithPopup: authMod.signInWithPopup,
      GoogleAuthProvider: authMod.GoogleAuthProvider,
      signOut: authMod.signOut,
    };
  })();
  return cached;
}
