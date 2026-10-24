import { expect, test } from "@playwright/test";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`opens the tea welcome without an HMR connection at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await page.routeWebSocket(/\/_next\/hmr(?:\?|$)/, (socket) =>
      socket.close(),
    );
    await page.route("**/api/tea/bootstrap", (route) =>
      route.fulfill({ status: 401, json: {} }),
    );
    await page.goto("/tea");
    await expect(
      page.getByRole("button", { name: "进入演示工作台" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "登录我的账号" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
  });
}

test("offers a retry when workspace initialization stalls", async ({
  page,
}) => {
  let stalled = true;
  await page.route("**/api/tea/bootstrap", (route) => {
    if (!stalled) return route.fulfill({ status: 401, json: {} });
  });
  await page.goto("/tea");
  const error = page.getByRole("alert").filter({ hasText: "加载工作台超时" });
  await expect(error).toBeVisible({
    timeout: 20_000,
  });
  stalled = false;
  await page.getByRole("button", { name: "重试" }).click();
  await expect(error).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "进入演示工作台" }),
  ).toBeVisible();
});
