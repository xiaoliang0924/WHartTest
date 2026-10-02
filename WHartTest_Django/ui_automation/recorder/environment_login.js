'use strict';

async function firstVisible(locator) {
  const count = await locator.count().catch(() => 0);
  for (let index = 0; index < count; index++) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible().catch(() => false)) return candidate;
  }
  return null;
}

async function loginWithCredentials(page, params) {
  const url = String(params.url || '');
  const username = String(params.username || '');
  const password = String(params.password || '');
  const manualText = String(params.manual_button_text || '手动登录');
  let passwordField = null;
  let accountField = null;
  let stage = '准备登录';
  let formScope = page;
  let credentialsTouched = false;
  if (!url || !username || !password) {
    return { ok: false, error: '自动登录配置不完整，请检查登录页 URL、账号和密码。' };
  }

  const startedAt = Date.now();
  const redact = (value) => {
    let text = String(value || '');
    for (const secret of [username, password]) {
      if (secret) text = text.split(secret).join('[已隐藏]');
    }
    // Login URLs may contain redirect parameters or tokens; diagnostics only
    // need the origin and path to identify the page.
    text = text.replace(/https?:\/\/[^\s"'<>]+/g, (raw) => {
      try {
        const parsed = new URL(raw.replace(/[),.;]+$/, ''));
        return `${parsed.origin}${parsed.pathname}`;
      } catch (_) {
        return '[页面地址已隐藏]';
      }
    });
    return text.slice(0, 300);
  };
  const clearCredentials = async () => {
    if (!credentialsTouched) return true;
    let cleared = true;
    for (const field of [passwordField, accountField]) {
      if (!field) continue;
      try {
        // A detached form cannot appear in a screenshot of the current page.
        if (await field.count() === 0) continue;
        await field.fill('', { timeout: 1000 });
      } catch (_) { cleared = false; }
    }
    if (cleared) credentialsTouched = false;
    return cleared;
  };
  const failure = async (summary, cause) => {
    const credentialsCleared = await clearCredentials();
    const pageState = { page_url: '', page_title: '', screenshot: '' };
    try {
      pageState.page_url = redact(page.url());
      pageState.page_title = redact(await page.title());
    } catch (_) {}
    if (credentialsCleared) {
      try {
        const shot = await page.screenshot({ type: 'jpeg', quality: 60 });
        pageState.screenshot = 'data:image/jpeg;base64,' + shot.toString('base64');
      } catch (_) {}
    }
    const detail = cause && (cause.message || String(cause));
    const detailText = detail ? `；执行器信息：${redact(detail)}` : '';
    const error = `登录阶段「${stage}」失败：${summary}${detailText}`;
    return {
      ok: false,
      error,
      state: {
        login_ok: false,
        stage,
        ...pageState,
        duration: (Date.now() - startedAt) / 1000,
      },
    };
  };
  try {
    stage = '打开登录页';
    if (page.url() !== url) {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    }

    stage = '等待登录表单';
    const passwordSelector = params.password_selector || 'input[type="password"]';
    const formDeadline = Date.now() + 15000;
    let manualClicked = false;
    // SSO may render its form in a child frame. Inspect every attached frame,
    // including nested/cross-origin frames, instead of binding to the main page.
    while (Date.now() < formDeadline) {
      for (const scope of [page, ...page.frames().filter(f => f !== page.mainFrame())]) {
        const candidate = await firstVisible(scope.locator(String(passwordSelector)));
        if (candidate) {
          formScope = scope;
          passwordField = candidate;
          break;
        }
      }
      if (passwordField) break;
      // Wait for hydration too: the entry button may appear after DOMContentLoaded.
      if (!manualClicked && manualText) {
        for (const locator of [
          page.getByRole('button', { name: manualText, exact: true }),
          page.getByRole('link', { name: manualText, exact: true }),
          page.getByText(manualText, { exact: true }),
        ]) {
          const entry = await firstVisible(locator);
          if (entry) {
            stage = '点击登录切换入口';
            await entry.click({ timeout: 10000 });
            manualClicked = true;
            break;
          }
        }
      }
      stage = '等待登录表单';
      await page.waitForTimeout(100);
    }
    if (!passwordField) {
      return await failure('未找到可见的密码输入框（已检查主页面及嵌入登录页面），请核对登录入口和密码定位器。');
    }

    stage = '查找账号输入框';
    if (params.account_selector) {
      accountField = await firstVisible(formScope.locator(String(params.account_selector)));
    } else {
      const candidates = [
        formScope.getByLabel(/账号|用户名|用户|手机号|工号|account|username/i)
          .and(formScope.locator('input:not([type="password"]), textarea')),
        formScope.getByPlaceholder(/账号|用户名|用户|手机号|工号|account|username/i),
        formScope.locator('input[type="text"], input[type="tel"], input:not([type])'),
      ];
      for (const candidate of candidates) {
        const visible = await firstVisible(candidate);
        if (visible) {
          accountField = visible;
          break;
        }
      }
    }
    if (!accountField || !(await accountField.isVisible().catch(() => false))) {
      return await failure('未找到可见的账号输入框，请配置账号输入框定位器。');
    }

    stage = '填写账号和密码';
    credentialsTouched = true;
    await accountField.fill(username, { timeout: 10000 });
    await passwordField.fill(password, { timeout: 10000 });

    stage = '查找登录按钮';
    let submitButton;
    if (params.submit_selector) {
      submitButton = await firstVisible(formScope.locator(String(params.submit_selector)));
    } else {
      submitButton = await firstVisible(formScope.getByRole('button', {
        name: /^(?:登录|登录并继续|确定登录|sign\s*in|log\s*in|submit)$/i,
      }));
    }
    if (!submitButton) {
      return await failure('未找到可见的登录按钮，请配置登录按钮定位器。');
    }
    stage = '提交登录';
    await submitButton.click({ timeout: 10000 });

    stage = '验证登录成功';
    if (params.success_selector) {
      await page.locator(String(params.success_selector)).first().waitFor({
        state: 'visible', timeout: 30000,
      });
    } else {
      await page.waitForFunction(() => {
        const visible = (el) => {
          const style = window.getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return style.display !== 'none' && style.visibility !== 'hidden'
            && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
        };
        const path = new URL(window.location.href).pathname;
        const onLoginRoute = /\/(login|signin|sign-in|auth)(\/|$)/i.test(path);
        const passwordVisible = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
        const authenticatedShellVisible = Array.from(
          document.querySelectorAll('nav,[role="navigation"],aside')
        ).some(visible);
        const loginDialogVisible = Array.from(document.querySelectorAll('[role="dialog"] iframe')).some(visible);
        return !passwordVisible && !loginDialogVisible && (!onLoginRoute || authenticatedShellVisible);
      }, null, { timeout: 30000 });
    }

    // 清理输入值，避免后续 Trace、截图或录制动作保留凭据。
    await clearCredentials();
    return {
      ok: true,
      state: {
        message: '账号登录成功，已进入业务系统',
        page_url: page.url(),
        duration: (Date.now() - startedAt) / 1000,
      },
    };
  } catch (error) {
    // Preserve the failure stage, redacted Playwright diagnostic, and a
    // credential-cleared screenshot so the execution record can be diagnosed.
    return await failure('请核对页面流程、定位配置和登录成功标志。', error);
  } finally {
    await clearCredentials();
  }
}

module.exports = { loginWithCredentials };
