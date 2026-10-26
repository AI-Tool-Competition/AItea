import { expect, test } from "@playwright/test";

import {
  type ApiSettings,
  type ApiSettingsPatch,
} from "../../src/core/tea/types";

// CI uses its installed Chromium; local runs can reuse an existing browser.
test.use({
  launchOptions: { executablePath: process.env.TEA_BROWSER_EXECUTABLE },
});

const initial: ApiSettings = {
  revision: "a".repeat(64),
  agent: { base_url: "", model: "", api_key_configured: false },
  embedding: { base_url: "", model: "", api_key_configured: false },
  rerank: { base_url: "", model: "", api_key_configured: false },
  mineru: { api_key_configured: false },
  agent_inherits_api_key: false,
  embedding_inherits_api_key: false,
  environment_overrides: [],
  restart_required: false,
};
const bootstrap = {
  user: {
    id: "settings-admin",
    name: "管理员",
    email: "admin@example.test",
    is_demo: false,
    can_login: true,
    is_admin: true,
  },
  templates: [],
  cases: [],
  admin_setup_required: false,
  capabilities: {
    assistant_mode: "demo",
    retrieval_mode: "unavailable",
    parser: "mineru",
  },
};

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`admin can configure all provider types at ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    let current = structuredClone(initial);
    let submitted: ApiSettingsPatch | undefined;
    await page.route("**/api/tea/bootstrap", (route) =>
      route.fulfill({ json: bootstrap }),
    );
    await page.route("**/api/tea/admin/api-settings", async (route) => {
      if (route.request().method() === "PATCH") {
        submitted = route.request().postDataJSON() as ApiSettingsPatch;
        current = {
          ...current,
          revision: "b".repeat(64),
          restart_required: true,
        };
        for (const id of ["agent", "embedding", "rerank"] as const) {
          current[id] = {
            base_url: submitted[id]?.base_url ?? "",
            model: submitted[id]?.model ?? "",
            api_key_configured: true,
          };
        }
        current.mineru = { api_key_configured: true };
      }
      await route.fulfill({ json: current });
    });
    await page.goto("/tea/admin/api");
    await expect(
      page.getByRole("heading", { name: "API 配置", level: 2 }),
    ).toBeVisible();
    if (viewport.width < 760)
      await page.getByRole("button", { name: "展开后台菜单" }).click();
    await expect(
      page.getByRole("link", { name: "API 配置", exact: true }),
    ).toBeVisible();
    await page.getByRole("link", { name: "API 配置", exact: true }).click();

    for (const title of ["Agent", "Embedding", "Rerank"]) {
      await page
        .getByLabel(`${title} Base URL`, { exact: true })
        .fill(`https://${title.toLowerCase()}.example/v1`);
      await page
        .getByLabel(`${title} 调用模型`, { exact: true })
        .fill(`${title.toLowerCase()}-model`);
      await page
        .getByLabel(`${title} API Key`, { exact: true })
        .fill(`fixture-${title}-key`);
    }
    await page
      .getByLabel("MinerU API Key", { exact: true })
      .fill("fixture-mineru-key");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await expect(page.getByText(/配置已保存。修改检索模型/)).toBeVisible();
    expect(submitted?.expected_revision).toBe(initial.revision);
    expect(submitted?.agent?.model).toBe("agent-model");
    expect(submitted?.embedding?.base_url).toBe("https://embedding.example/v1");
    expect(submitted?.rerank?.api_key).toBe("fixture-Rerank-key");
    expect(submitted?.mineru?.api_key).toBe("fixture-mineru-key");
    for (const title of ["Agent", "Embedding", "Rerank", "MinerU"]) {
      await expect(
        page.getByLabel(`${title} API Key`, { exact: true }),
      ).toHaveValue("");
    }
    await page.reload();
    await expect(
      page.getByLabel("Agent 调用模型", { exact: true }),
    ).toHaveValue("agent-model");
    await expect(page.getByText(/等待重启生效/)).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`api-settings-${viewport.width}.png`),
      fullPage: true,
    });
  });
}

test("ordinary accounts cannot enter API settings", async ({ page }) => {
  let settingsReads = 0;
  await page.route("**/api/tea/bootstrap", (route) =>
    route.fulfill({
      json: { ...bootstrap, user: { ...bootstrap.user, is_admin: false } },
    }),
  );
  await page.route("**/api/tea/admin/api-settings", (route) => {
    settingsReads++;
    return route.fulfill({ status: 403, json: {} });
  });
  await page.goto("/tea/admin/api");
  await expect(
    page.getByText(
      "当前账号没有管理员权限。可以切换到管理员账号，或返回工作台。",
    ),
  ).toBeVisible();
  expect(settingsReads).toBe(0);
});
