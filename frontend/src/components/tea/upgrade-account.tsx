"use client";
import { ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import { errorMessage, post } from "@/core/tea/api";
import { type User } from "@/core/tea/types";

import { ActionButton, ErrorNotice } from "./shared";

export function UpgradeAccount({
  user,
  onUpdated,
}: {
  user: User;
  onUpdated: (message?: string) => void;
}) {
  const [name, setName] = useState(user.name);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  async function upgrade() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const updated = await post<User>("/auth/register", {
        name,
        email,
        password,
      });
      onUpdated(
        updated.can_login
          ? undefined
          : "账号已创建，等待管理员开放登录。获准后可用此邮箱和密码登录。",
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="tea-config-intro" style={{ maxWidth: 700 }}>
      <div className="tea-panel-label">
        <ShieldCheck size={17} />
        注册账号，保留当前业务
      </div>
      <p className="tea-small-note">
        直接将当前演示账号升级，业务、资料、文稿和办理记录一并保留。账号需经管理员开放后才能登录。
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void upgrade();
        }}
      >
        <fieldset disabled={busy}>
          <div className="tea-form-row">
            <label className="tea-field">
              <span>姓名</span>
              <Input
                required
                maxLength={80}
                autoComplete="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label className="tea-field">
              <span>邮箱</span>
              <Input
                required
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
          </div>
          <label className="tea-field">
            <span>设置密码 · 至少 10 个字符</span>
            <Input
              type="password"
              required
              minLength={10}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && <ErrorNotice message={error} />}
          <ActionButton busy={busy} type="submit">
            注册并保留当前业务
          </ActionButton>
        </fieldset>
      </form>
    </div>
  );
}
