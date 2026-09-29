import { initializeApp, getApps } from "firebase/app";
import { getFirestore } from "firebase/firestore";

export const firebaseConfig = {
  apiKey: "AIzaSyDBahzBvKxk_A8jgSlLr7f5VDg5qsGHXQA",
  authDomain: "restro-qr-fd790.firebaseapp.com",
  projectId: "restro-qr-fd790",
  storageBucket: "restro-qr-fd790.firebasestorage.app",
  messagingSenderId: "773950112766",
  appId: "1:773950112766:web:5bdbb0b80dbe50ce9af6af",
};

export const firebaseApp = getApps().length ? getApps()[0] : initializeApp(firebaseConfig);
export const db = getFirestore(firebaseApp);

if (typeof window !== "undefined") {
  (window as typeof window & {
    __MENUQR_FIREBASE__?: { app: typeof firebaseApp; db: typeof db };
  }).__MENUQR_FIREBASE__ = {
    app: firebaseApp,
    db,
  };
}
