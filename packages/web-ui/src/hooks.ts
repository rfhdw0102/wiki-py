"use client";

import { useCallback, useEffect, useState } from "react";

export function useApiResource<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    load()
      .then((value) => {
        if (active) setData(value);
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(cause instanceof Error ? cause.message : "The request failed.");
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [load, revision]);

  const reload = useCallback(() => setRevision((value) => value + 1), []);
  return { data, error, loading, reload, setData };
}
