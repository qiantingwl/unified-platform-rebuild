import { test, expect } from "@playwright/test";

const fixture = "/tests/dialog-fixture.html";

test("账户创建：取消无写入，重复提交只发一次，失败保留输入", async ({
  page,
}) => {
  let calls = 0;
  let release;
  await page.route("**/api/accounts", async (route) => {
    calls += 1;
    if (calls === 1) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "identity_conflict" }),
      });
      return;
    }
    await new Promise((resolve) => {
      release = resolve;
    });
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ id: "demo-account" }),
    });
  });
  await page.goto(fixture);
  await page.getByRole("button", { name: "打开账户创建" }).click();
  await page.getByRole("button", { name: "取消" }).click();
  expect(calls).toBe(0);
  await page.getByRole("button", { name: "打开账户创建" }).click();
  await page.getByText("其他方式", { exact: true }).click();
  await page.getByRole("button", { name: "仅手动建立账户记录" }).click();
  await page.getByLabel("平台").selectOption("demo-platform");
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByLabel("账户名称").fill("隔离样本");
  await page.getByLabel("平台账户 ID").fill("sample_01");
  await page.getByRole("button", { name: "创建账户记录" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("账户名称")).toHaveValue("隔离样本");
  await page.getByRole("button", { name: "创建账户记录" }).dblclick();
  await expect(page.getByRole("button", { name: "创建中…" })).toBeDisabled();
  await expect.poll(() => calls).toBe(2);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeVisible();
  release();
  await expect(page.getByText("账户记录已建立。还需配置凭证")).toBeVisible();
  await expect(page.getByTestId("refresh-count")).toHaveText("1");
  expect(calls).toBe(2);
  await page.getByRole("button", { name: "完成" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("密钥创建：失败不清空，提交锁与一次性展示", async ({ page }) => {
  let calls = 0;
  let release;
  await page.route("**/api/keys", async (route) => {
    calls += 1;
    if (calls === 1) {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: "invalid_input" }),
      });
      return;
    }
    await new Promise((resolve) => {
      release = resolve;
    });
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ token: "local-fixture-token" }),
    });
  });
  await page.goto(fixture);
  await page.getByRole("button", { name: "打开密钥创建" }).click();
  await page.getByLabel("平台").selectOption("demo-platform");
  await page.getByLabel("密钥名称").fill("本地样本");
  await page.getByLabel("默认模型").fill("demo-model");
  await page.getByRole("button", { name: "创建密钥" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("密钥名称")).toHaveValue("本地样本");
  await page.getByRole("button", { name: "创建密钥" }).dblclick();
  await expect.poll(() => calls).toBe(2);
  await expect(page.getByRole("button", { name: "关闭弹窗" })).toBeDisabled();
  release();
  await expect(page.getByLabel("新密钥")).toHaveValue("local-fixture-token");
  await expect(page.getByTestId("refresh-count")).toHaveText("1");
  await page.getByRole("button", { name: "完成" }).click();
  await page.getByRole("button", { name: "打开密钥创建" }).click();
  await expect(page.getByLabel("新密钥")).toHaveCount(0);
  expect(calls).toBe(2);
});
