---
name: playwright-skill
description: 浏览器自动化执行工具。用于执行 Web 页面测试、表单填写、登录验证、截图等浏览器操作。
---

# Playwright 浏览器自动化

执行浏览器自动化任务，支持页面测试、表单操作、登录验证等。

## ⚠️ 强制规则（违反会导致退出码 1）

### 1. 多步骤必须用同一个 session_id

执行测试用例、登录后继续操作时，**每一步** `execute_skill_script` 都必须带相同 `session_id`（建议 `case_{用例ID}`）。

- 持久化模式下直接使用已有 `page`，**禁止** `chromium.launch()` / `browser.newPage()` / `browser.close()`
- 不要每步新开浏览器（会反复登录，弹窗反复出现，登录态丢失）

### 2. 登录或跳转后先关遮挡弹窗

目标系统登录后常弹出「发现新版本」，不关掉则后续点击全部被拦截（`intercepts pointer events` → 退出码 1）。

```javascript
await helpers.dismissBlockingDialogs(page);
```

只关「我知道了」这类一次性提示，不要点业务弹窗的「确定/取消」。

### 3. 禁止使用会变的选择器

**禁止** `#el-id-*`、`#el-id-847-3` 等 Element Plus 自动生成 ID（每次刷新都变）。

优先使用：

```javascript
page.getByRole('button', { name: '批量操作' })
page.getByPlaceholder('请输入用户名')
page.getByText('我的工单', { exact: true })
```

打开页面后调用 `helpers.describePageForAI(page)` 获取结构，再操作。**禁止猜测选择器，禁止靠截图认元素。**

### 4. Docker / 后台执行必须无头

```javascript
chromium.launch({ headless: true })
```

`headless: false` 在容器里会失败。有 `session_id` 时不要自己 launch，系统已按无头启动。

### 5. 产品不符合预期 ≠ 脚本崩溃

定位超时、语法错误可以让脚本失败。  
**断言没过**（例如按钮该 disabled 却仍可点）时：

1. `console.log('RESULT=FAIL: ...原因...')`
2. 截图并 `upload_screenshot`
3. **禁止** `throw` / `process.exit(1)`（界面会显示成「命令执行失败」，不像用例失败）

通过时输出 `RESULT=PASS`。

---

## 使用方法

通过 `execute_skill_script` 调用，传入 inline 代码：

```
node run.js "your playwright code here"
```

代码必须写在一行，用分号分隔。run.js 会自动包装 async IIFE 和 require。**禁止** `--session`、`--inline`、`--eval`。

也可以只传裸 JS（以 `const`/`await` 开头），系统会自动包成 `node run.js '...'`。

## 截图路径约定

**必须使用 `process.env.SCREENSHOT_DIR`**。命名建议：`case_{case_id}_step{step_number}.png`

```javascript
const dir = process.env.SCREENSHOT_DIR;
await page.screenshot({ path: `${dir}/case_11_step1.png` });
```

## 先获取页面结构，再操作元素

```javascript
await page.goto('http://example.com');
await helpers.dismissBlockingDialogs(page);
const desc = await helpers.describePageForAI(page);
console.log(desc);
```

然后用返回的稳定选择器或 getByRole / getByPlaceholder 操作。

## 持久化会话模式（用例执行默认用这个）

### 核心规则

1. **session_id 全程一致**，否则会开多个浏览器
2. **直接用 `page`**，不要 `chromium.launch()`
3. **不要 `browser.close()`**，空闲 15 分钟自动关

### 示例

**步骤 1：打开并登录**
```
skill_name="playwright-skill"
session_id="case_1354"
command='node run.js "const dir = process.env.SCREENSHOT_DIR; await page.goto(\'http://test.bot.by56.com/work-order/login\', { waitUntil: \'networkidle\' }); await page.getByPlaceholder(\'请输入用户名\').fill(\'19902579992\'); await page.getByPlaceholder(\'请输入密码\').fill(\'000000\'); await page.getByRole(\'button\', { name: \'登 录\' }).click(); await page.waitForLoadState(\'networkidle\'); await helpers.dismissBlockingDialogs(page); await page.screenshot({ path: dir + \'/case_1354_login.png\' });"'
```

**步骤 2：同一浏览器继续操作**
```
skill_name="playwright-skill"
session_id="case_1354"
command='node run.js "await helpers.dismissBlockingDialogs(page); const bulkBtn = page.getByRole(\'button\', { name: \'批量操作\' }); console.log(\'isDisabled=\', await bulkBtn.isDisabled());"'
```

### 持久化 vs 非持久化

| 特性 | 无 session_id | 有 session_id |
|------|---------------|---------------|
| 浏览器 | 代码自己 launch/close | 系统管理，直接用 `page` |
| 跨步骤登录态 | 不保持 | 保持 |
| 适用 | 单步探查 | **测试用例、多步骤操作** |

## 非持久化单步示例（必须 headless: true）

```
node run.js "const dir = process.env.SCREENSHOT_DIR; const browser = await chromium.launch({ headless: true }); const page = await browser.newPage(); await page.goto('http://example.com'); await helpers.dismissBlockingDialogs(page); await page.screenshot({ path: dir + '/example.png' }); await browser.close();"
```

## 其他 helpers

- `helpers.dismissBlockingDialogs(page)`：关掉「发现新版本」等遮挡弹窗
- `helpers.describePageForAI(page)`：可读的页面元素列表
- `helpers.getPageStructure(page)`：结构化 JSON
- `helpers.getPageText(page)`：可见文本

## 常用定位（推荐）

```javascript
await page.getByPlaceholder('请输入用户名').fill('admin');
await page.getByRole('button', { name: '登 录' }).click();
await page.getByRole('button', { name: '批量操作' }).click();
await page.getByText('我的工单', { exact: true }).click();
```

不要用：`#el-id-7201-8`、`#el-id-847-3`

## 注意事项

1. 截图路径必须用 `process.env.SCREENSHOT_DIR`
2. inline 代码一行、分号分隔；字符串内双引号转义 `\"`
3. 有 session_id 时不要 close 浏览器
4. 用 `console.log()` 输出进度和 `RESULT=PASS/FAIL`
5. Docker 里必须 `headless: true`
