import { test, expect } from "@playwright/test";

const fixtureAccount = (id) => ({
  id: `account-${id}`,
  platform: "Loomy",
  display_name: `Account ${id}`,
  label: `Account ${id}`,
  identity_hint: "ab…123",
  status: "ready",
  enabled: true,
  balance: 8,
  balance_state: "verified",
  balance_at: 1795100000,
  updated_at: 1795100000,
  credential_version: 1,
  model_blocks: [],
});

async function mockConsole(page, handlers = {}) {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/events")
      return route.fulfill({
        contentType: "text/event-stream",
        body: ": idle\n\n",
      });
    const data =
      handlers[url.pathname]?.(url, route.request()) ??
      {
        "/api/snapshot": {
          revision: 1,
          as_of: 1795100000,
          business_day: "2026-11-19",
          platforms: ["Loomy", "WPS 灵犀"],
          runtime: { version: "0.3.0" },
          adapter_catalog: { Loomy: { network_enabled: true } },
        },
        "/api/model-directory": { total: 0, items: [] },
        "/api/model-routes/Loomy": { mappings: [], groups: [] },
        "/api/dispatch-policies": {
          policies: [{ platform: "Loomy", strategy: "lru" }],
        },
        "/api/proxy-subscriptions": { subscriptions: [] },
        "/api/proxy-kernel": {
          enabled: true,
          running: true,
          listeners_ready: true,
        },
      }[url.pathname] ??
      {};
    return route.fulfill({ json: data });
  });
}

async function expectPageFits(page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      ),
    )
    .toBeLessThanOrEqual(1);
}

test("account navigation reads the requested page and resets filters to page one", async ({
  page,
}) => {
  const calls = [];
  await mockConsole(page, {
    "/api/accounts": (u) => {
      const q = u.searchParams;
      const number = Number(q.get("page"));
      calls.push({ number, platform: q.get("platform") });
      return {
        page: number,
        total: 51,
        items: [fixtureAccount(number)],
        summary: { total: 51, ready: 51, busy: 0, unavailable: 0 },
        status_counts: { ready: 51 },
      };
    },
  });
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/#accounts");
  await expect(page.getByRole("button", { name: /^Account 1 / })).toBeVisible();
  await expectPageFits(page);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByRole("button", { name: /^Account 2 / })).toBeVisible();
  await page.getByLabel("平台筛选").selectOption("Loomy");
  await expect(page.getByRole("button", { name: /^Account 1 / })).toBeVisible();
  expect(calls).toEqual([
    { number: 1, platform: null },
    { number: 2, platform: null },
    { number: 1, platform: "Loomy" },
  ]);
  await page.getByRole("button", { name: "展开工作台控制" }).click();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect.poll(() => calls.length).toBe(4);
  expect(calls.at(-1)).toEqual({ number: 1, platform: "Loomy" });
});

test("model sync uses the scoped picker directly and opens a single dialog", async ({
  page,
}) => {
  let picker;
  let sent;
  await mockConsole(page, {
    "/api/accounts/picker": (u) => {
      picker = Object.fromEntries(u.searchParams);
      return { total: 1, page: 1, items: [fixtureAccount(1)] };
    },
    "/api/accounts/account-1/models/sync": (_, request) => {
      sent = request.method();
      return { models: ["model-1"] };
    },
  });
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/#models");
  await expect(
    page.getByText("尚未同步模型。", { exact: false }),
  ).toBeVisible();
  await expectPageFits(page);
  await page.getByRole("button", { name: "同步模型", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveCount(1);
  await dialog.getByRole("button", { name: /Account 1/ }).click();
  expect(picker).toMatchObject({
    platform: "Loomy",
    capability: "models",
    page: "1",
  });
  await dialog.getByRole("button", { name: "同步官方模型" }).click();
  await expect(dialog).toHaveCount(0);
  expect(sent).toBe("POST");
  await page.getByRole("link", { name: "管理模型映射" }).click();
  await expect(page.getByRole("heading", { name: "路由配置" })).toBeVisible();
  await expectPageFits(page);
});

test("key and proxy directories stay inside the viewport at console widths", async ({
  page,
}) => {
  await mockConsole(page, {
    "/api/keys": () => ({
      page: 1,
      total: 1,
      items: [
        {
          id: "key-1",
          name: "一条有较长名称的密钥",
          platform: "Loomy",
          enabled: true,
          policy: {
            models: [
              "very-long-model-identifier-which-must-not-expand-the-document",
            ],
          },
        },
      ],
    }),
    "/api/proxy-directory": () => ({
      total: 1,
      nodes: [
        {
          id: "p1",
          name: "Node 1",
          version: 1,
          address: "http://127.0.0.1:12345",
          data: { status: "untested", scheme: "ss" },
        },
      ],
    }),
    "/api/proxy-target-checks": () => ({
      checks: [
        {
          data: {
            proxy_id: "p1",
            node_version: 1,
            status: "completed",
            checked_at: Date.now() / 1000,
            eligible: true,
            http_status: 404,
          },
        },
      ],
    }),
  });
  for (const width of [390, 1024, 1366]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/#keys");
    await expect(
      page.getByRole("cell", { name: /^一条有较长名称的密钥 …$/ }),
    ).toBeVisible();
    await expectPageFits(page);
    await page.goto("/#proxy");
    await expect(
      page.getByRole("cell", { name: /^目标网站已响应 · 未测模型/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "复制无认证地址" }),
    ).toHaveCount(0);
    await expectPageFits(page);
  }
});
