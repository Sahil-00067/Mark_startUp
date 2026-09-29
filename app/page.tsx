"use client";

import { useEffect, useRef, useState } from "react";
import { legacyBodyHtml, legacyCss, legacyScript } from "./legacyData";

const LEGACY_BOOT_GUARD = "__menuqr_legacy_boot__";

export default function Home() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [backendReady, setBackendReady] = useState(false);

  useEffect(() => {
    if (!rootRef.current || typeof document === "undefined") return;

    if (typeof window !== "undefined" && (window as typeof window & { [LEGACY_BOOT_GUARD]?: boolean })[LEGACY_BOOT_GUARD]) {
      return;
    }

    let styleTag = document.getElementById("legacy-website-style") as HTMLStyleElement | null;
    if (!styleTag) {
      styleTag = document.createElement("style");
      styleTag.id = "legacy-website-style";
      document.head.appendChild(styleTag);
    }
    styleTag.textContent = legacyCss;

    const boot = async () => {
      try {
        if (typeof window !== "undefined") {
          (window as typeof window & { [LEGACY_BOOT_GUARD]?: boolean })[LEGACY_BOOT_GUARD] = true;
        }

        const response = await fetch("/api/menu");
        if (response.ok) {
          setBackendReady(true);
        }

        const script = document.createElement("script");
        script.textContent = legacyScript;
        document.body.appendChild(script);
      } catch (error) {
        console.warn("Legacy page boot warning:", error);
        if (typeof window !== "undefined") {
          delete (window as typeof window & { [LEGACY_BOOT_GUARD]?: boolean })[LEGACY_BOOT_GUARD];
        }
      }
    };

    void boot();
  }, []);

  return (
    <>
      <div ref={rootRef} dangerouslySetInnerHTML={{ __html: legacyBodyHtml }} />
      {backendReady ? null : null}
    </>
  );
}
