import { expect, test } from "@playwright/test";

const basePath = process.env.PLAYWRIGHT_TEA_BASE_PATH ?? "";
const entry = basePath || "/";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`opens the inline tea login at the root without HMR at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await page.routeWebSocket(/\/_next\/hmr(?:\?|$)/, (socket) =>
      socket.close(),
    );
    await page.route("**/api/tea/bootstrap", (route) =>
      route.fulfill({ status: 401, json: {} }),
    );
    await page.goto(entry);
    await expect(
      page.getByRole("heading", { name: "欢迎回到茶渡" }),
    ).toBeVisible();
    await expect(page.getByLabel("邮箱或账号", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.getByRole("button", { name: "创建账号", exact: true }).click();
    await expect(page.getByLabel("姓名", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "创建账号并进入" }),
    ).toBeVisible();
    await expect(page).toHaveURL((url) => url.pathname === entry);
  });
}

test("redirects the previous workbench entry to the root", async ({ page }) => {
  await page.route("**/api/tea/bootstrap", (route) =>
    route.fulfill({ status: 401, json: {} }),
  );
  await page.goto(`${basePath}/tea`);
  await expect(page).toHaveURL((url) => url.pathname === entry);
  await expect(page.getByRole("form", { name: "账号登录" })).toBeVisible();
});

test("offers a retry when workspace initialization stalls", async ({
  page,
}) => {
  let stalled = true;
  await page.route("**/api/tea/bootstrap", (route) => {
    if (!stalled) return route.fulfill({ status: 401, json: {} });
  });
  await page.goto(entry);
  const error = page.getByRole("alert").filter({ hasText: "加载工作台超时" });
  await expect(error).toBeVisible({
    timeout: 20_000,
  });
  stalled = false;
  await page.getByRole("button", { name: "重试" }).click();
  await expect(error).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "登录", exact: true }),
  ).toBeVisible();
});
