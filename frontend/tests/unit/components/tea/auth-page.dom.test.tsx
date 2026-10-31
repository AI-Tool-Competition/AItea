import { afterEach, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AuthPage } from "@/components/tea/auth-page";

const account = {
  id: "account",
  name: "业务员",
  email: "owner@example.com",
  can_login: true,
  is_admin: false,
  is_demo: false,
};

afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

it("opens an inline login form and preserves credentials after a failed login", async () => {
  const authenticated = rs.fn();
  const fetcher = rs
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      Response.json(
        { error: { code: "invalid_credentials", message: "邮箱或密码不正确" } },
        { status: 401 },
      ),
    )
    .mockResolvedValueOnce(Response.json(account));
  render(
    <AuthPage
      onAuthenticated={authenticated}
      onRetry={rs.fn()}
      initialError=""
    />,
  );
  expect(screen.queryByRole("dialog")).toBeNull();
  const email = screen.getByLabelText<HTMLInputElement>("邮箱或账号");
  const password = screen.getByLabelText<HTMLInputElement>("密码", {
    exact: true,
  });
  fireEvent.change(email, { target: { value: "owner@example.com" } });
  fireEvent.change(password, { target: { value: "incorrect-password" } });
  fireEvent.submit(screen.getByRole("form", { name: "账号登录" }));
  await screen.findByRole("alert");
  expect(authenticated).not.toHaveBeenCalled();
  expect(email.value).toBe("owner@example.com");
  expect(password.value).toBe("incorrect-password");
  fireEvent.click(screen.getByRole("button", { name: "显示密码" }));
  expect(password.type).toBe("text");
  fireEvent.change(password, { target: { value: "correct-password" } });
  fireEvent.submit(screen.getByRole("form", { name: "账号登录" }));
  await waitFor(() => expect(authenticated).toHaveBeenCalledTimes(1));
  expect(fetcher).toHaveBeenLastCalledWith(
    "/api/tea/auth/login",
    expect.objectContaining({
      body: JSON.stringify({
        email: "owner@example.com",
        password: "correct-password",
      }),
      credentials: "same-origin",
    }),
  );
});

it("registers and enters immediately, while preventing duplicate submission", async () => {
  let complete!: (response: Response) => void;
  const fetcher = rs.spyOn(globalThis, "fetch").mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        complete = resolve;
      }),
  );
  const authenticated = rs.fn();
  render(
    <AuthPage
      onAuthenticated={authenticated}
      onRetry={rs.fn()}
      initialError=""
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "创建账号" }));
  fireEvent.change(screen.getByLabelText("姓名"), {
    target: { value: " 业务员 " },
  });
  fireEvent.change(screen.getByLabelText("邮箱", { exact: true }), {
    target: { value: "owner@example.com" },
  });
  const password = screen.getByLabelText<HTMLInputElement>("密码", {
    exact: true,
  });
  expect(password.autocomplete).toBe("new-password");
  expect(password.minLength).toBe(10);
  fireEvent.change(password, { target: { value: "correct-password" } });
  const form = screen.getByRole("form", { name: "创建账号" });
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(
    screen.getByRole<HTMLButtonElement>("button", { name: "返回登录" })
      .disabled,
  ).toBe(true);
  expect(fetcher).toHaveBeenCalledWith(
    "/api/tea/auth/register",
    expect.objectContaining({
      body: JSON.stringify({
        email: "owner@example.com",
        password: "correct-password",
        name: "业务员",
      }),
    }),
  );
  complete(Response.json(account, { status: 201 }));
  await waitFor(() => expect(authenticated).toHaveBeenCalledTimes(1));
});

it("keeps startup retry available and the admin login separate from registration", () => {
  const retry = rs.fn();
  const back = rs.fn();
  render(
    <AuthPage
      adminMode
      onAuthenticated={rs.fn()}
      onRetry={retry}
      initialError="加载工作台超时"
      onBack={back}
    />,
  );
  expect(screen.getByRole("heading", { name: "管理员登录" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "创建账号" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(retry).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "返回工作台" }));
  expect(back).toHaveBeenCalledTimes(1);
});
