"use client";

import { useEffect, useState } from "react";

import { errorMessage, teaRequest } from "./api";
import { type CollectionBlueprintSummary, type ListResult } from "./types";

export function countryLabel(country: string) {
  return (
    (
      { ID: "印尼", SG: "新加坡", unassigned: "未分国" } as Record<
        string,
        string
      >
    )[country] ?? country
  );
}

// Read only the available plan metadata. Never infer a document country from tags.
export function useCollectionCountries(ownerId?: string, enabled = true) {
  const [data, setData] = useState<{ ownerId?: string; countries: string[] }>();
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const abort = new AbortController();
    setError("");
    teaRequest<ListResult<CollectionBlueprintSummary>>("/collection-plans", {
      signal: abort.signal,
    })
      .then((result) => {
        if (abort.signal.aborted) return;
        const countries = [
          ...new Set(
            (Array.isArray(result.items) ? result.items : [])
              .map((plan) => plan.country)
              .filter(
                (country) =>
                  typeof country === "string" && /^[A-Z]{2}$/.test(country),
              ),
          ),
        ];
        setData({ ownerId, countries });
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      });
    return () => abort.abort();
  }, [ownerId, enabled, retry]);
  const ready = !enabled || (data?.ownerId === ownerId && !!data);
  const countries = ready ? (data?.countries ?? []) : [];
  return {
    countries,
    ready,
    defaultCountry: countries.includes("ID")
      ? "ID"
      : (countries[0] ?? "unassigned"),
    error,
    retry: () => setRetry((value) => value + 1),
  };
}
