"use client";

import { ArrowRight, Eye, EyeOff, LockKeyhole } from "lucide-react";
import { useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorMessage, post } from "@/core/tea/api";
import { type User } from "@/core/tea/types";

import { ActionButton, Brand, ErrorNotice } from "./shared";

export function AuthPage({
  onAuthenticated,
  onRetry,
  initialError,
  adminMode = false,
  onBack,
}: {
  onAuthenticated: () => void;
  onRetry: () => void;
  initialError: string;
  adminMode?: boolean;
  onBack?: () => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const pending = useRef(false);
  const id = useId();
  const registering = mode === "register";
  const demoEnabled = process.env.NODE_ENV !== "production";

  async function authenticate(action: "demo" | "login" | "register") {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const user = await post<User>(
        `/auth/${action}`,
        action === "demo"
          ? {}
          : {
              email: email.trim(),
              password,
              ...(action === "register" ? { name: name.trim() } : {}),
            },
      );
      if (!user.can_login) {
        setError("账号暂未开放登录，请联系管理员。");
        return;
      }
      onAuthenticated();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="tea-auth-page">
      <main>
        <div className="tea-auth-content">
          <div className="tea-auth-brand">
            <Brand />
          </div>
          <Card className="tea-auth-card">
            <CardHeader className="tea-auth-heading">
              <h1>
                {adminMode
                  ? "管理员登录"
                  : registering
                    ? "创建你的账号"
                    : "欢迎回到茶渡"}
              </h1>
              <CardDescription>
                {adminMode
                  ? "登录茶渡管理后台。"
                  : registering
                    ? "从这里，开启你的出口业务。"
                    : "继续你的业务与对话。"}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form
                aria-label={registering ? "创建账号" : "账号登录"}
                aria-busy={busy}
                onSubmit={(event) => {
                  event.preventDefault();
                  void authenticate(mode);
                }}
              >
                <fieldset disabled={busy}>
                  {registering && (
                    <label className="tea-field">
                      <span>姓名</span>
                      <Input
                        name="name"
                        autoComplete="name"
                        placeholder="怎么称呼你"
                        maxLength={80}
                        required
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                      />
                    </label>
                  )}
                  <label className="tea-field" htmlFor={`${id}-identity`}>
                    <span>{registering ? "邮箱" : "邮箱或账号"}</span>
                    <Input
                      id={`${id}-identity`}
                      name={registering ? "email" : "username"}
                      type={registering ? "email" : "text"}
                      autoComplete={registering ? "email" : "username"}
                      autoCapitalize="none"
                      spellCheck={false}
                      placeholder={
                        registering ? "输入你的邮箱" : "输入邮箱或账号"
                      }
                      maxLength={254}
                      required
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                    />
                  </label>
                  <div className="tea-field">
                    <label htmlFor={`${id}-password`}>密码</label>
                    <div className="tea-auth-password">
                      <Input
                        id={`${id}-password`}
                        name="password"
                        type={showPassword ? "text" : "password"}
                        minLength={registering ? 10 : 1}
                        maxLength={128}
                        autoComplete={
                          registering ? "new-password" : "current-password"
                        }
                        aria-describedby={
                          registering ? `${id}-hint` : undefined
                        }
                        placeholder={registering ? "设置你的密码" : "输入密码"}
                        required
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="tea-auth-password-toggle"
                        aria-label={showPassword ? "隐藏密码" : "显示密码"}
                        aria-pressed={showPassword}
                        onClick={() => setShowPassword((visible) => !visible)}
                      >
                        {showPassword ? (
                          <EyeOff size={16} />
                        ) : (
                          <Eye size={16} />
                        )}
                      </Button>
                    </div>
                    {registering && (
                      <small id={`${id}-hint`}>至少 10 个字符</small>
                    )}
                  </div>
                  {error && <ErrorNotice message={error} />}
                  {initialError && (
                    <ErrorNotice message={initialError} onRetry={onRetry} />
                  )}
                  <ActionButton
                    type="submit"
                    busy={busy}
                    className="tea-auth-submit"
                  >
                    {adminMode
                      ? "登录管理后台"
                      : registering
                        ? "创建账号并进入"
                        : "登录"}
                    {!busy && <ArrowRight size={16} aria-hidden="true" />}
                  </ActionButton>
                </fieldset>
              </form>
            </CardContent>
            <CardFooter className="tea-auth-footer">
              {adminMode ? (
                onBack && (
                  <Button variant="link" disabled={busy} onClick={onBack}>
                    返回工作台
                  </Button>
                )
              ) : (
                <>
                  <span>{registering ? "已有账号？" : "还没有账号？"}</span>
                  <Button
                    variant="link"
                    disabled={busy}
                    onClick={() => {
                      setMode(registering ? "login" : "register");
                      setShowPassword(false);
                      setError("");
                    }}
                  >
                    {registering ? "返回登录" : "创建账号"}
                  </Button>
                </>
              )}
            </CardFooter>
          </Card>
          <p className="tea-auth-note">
            <LockKeyhole size={12} aria-hidden="true" />
            业务、材料与对话记录随账号保存
          </p>
          {demoEnabled && !adminMode && (
            <Button
              className="tea-auth-demo"
              variant="ghost"
              disabled={busy}
              onClick={() => void authenticate("demo")}
            >
              进入演示工作台
            </Button>
          )}
        </div>
      </main>
    </div>
  );
}
