"use client";

import { ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, post } from "@/core/tea/api";
import { type User } from "@/core/tea/types";

import { ErrorNotice } from "./shared";

export function AdminBootstrap({ onReady }: { onReady: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);

  async function submit() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      await post<User>("/admin/bootstrap", { bootstrap_token: token });
      onReady();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <form
      className="ta-admin-bootstrap"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="tea-field">
        <span>管理员引导码</span>
        <Input
          type="password"
          autoComplete="off"
          required
          maxLength={256}
          value={token}
          onChange={(event) => setToken(event.target.value)}
        />
      </label>
      <p className="tea-small-note">首次设置管理员时使用，请向部署负责人获取。</p>
      {error && <ErrorNotice message={error} />}
      <Button type="submit" disabled={busy || !token}>
        <ShieldCheck size={15} />
        {busy ? "正在设置…" : "设置为管理员"}
      </Button>
    </form>
  );
}
