"use client";

import { RequestState } from "@wiki-agent/web-ui";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function HomePage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/wiki");
  }, [router]);

  return <RequestState error={null} loading />;
}
