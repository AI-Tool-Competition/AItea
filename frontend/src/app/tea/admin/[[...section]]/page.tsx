import { notFound } from "next/navigation";

import { TeaApp } from "@/components/tea/tea-app";

export default async function AdminPage({
  params,
  searchParams,
}: {
  params: Promise<{ section?: string[] }>;
  searchParams: Promise<{ stage?: string }>;
}) {
  const { section } = await params;
  const page = section?.[0] ?? "workflow";
  if (
    (section?.length ?? 0) > 1 ||
    !["workflow", "analysis", "library", "sources"].includes(page)
  )
    notFound();
  const { stage } = await searchParams;
  return (
    <TeaApp
      initialAdminPage={page as "workflow" | "analysis" | "library" | "sources"}
      requestedStage={stage}
    />
  );
}
