#!/usr/bin/env node
'use strict';

/**
 * Playwright 持久化会话服务
 *
 * 协议：stdin/stdout 行分隔 JSON
 * 请求：
 *   - 心跳：{ id, method: "ping", params: {} }
 *   - 执行：{ id, method: "exec", params: { args: string[], env?: object } }
 *   - 关闭：{ id, method: "close", params: {} }
 *
 * 响应：
 *   - 响应结构：{ id, ok: boolean, stdout?: string[], stderr?: string[], error?: string, state?: object }
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const Module = require('module');
const { execSync } = require('child_process');

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function serverLog(...args) {
  try {
    process.stderr.write(args.map(String).join(' ') + '\n');
  } catch (_) {}
}

function parseCli(argv) {
  const out = { skillDir: '' };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--skill-dir') {
      out.skillDir = argv[i + 1] || '';
      i++;
    }
  }
  return out;
}

function checkPlaywrightInstalled(requireFromSkill) {
  try {
    requireFromSkill.resolve('playwright');
    return true;
  } catch (_) {
    return false;
  }
}

function installPlaywright(skillDir) {
  const allow = (process.env.PLAYWRIGHT_AUTO_INSTALL || 'true').toLowerCase() !== 'false';
  if (!allow) return false;

  serverLog('[playwright_persistent_server] Playwright not found. Installing...');
  try {
    execSync('npm install', { stdio: 'inherit', cwd: skillDir });
    execSync('npx playwright install chromium', { stdio: 'inherit', cwd: skillDir });
    serverLog('[playwright_persistent_server] Playwright installed successfully');
    return true;
  } catch (e) {
    serverLog('[playwright_persistent_server] Failed to install Playwright:', e?.message || String(e));
    return false;
  }
}

function createCapturedConsole() {
  const stdout = [];
  const stderr = [];

  const fmt = (args) =>
    args
      .map((a) => {
        if (typeof a === 'string') return a;
        try {
          return JSON.stringify(a);
        } catch (_) {
          return String(a);
        }
      })
      .join(' ');

  return {
    stdout,
    stderr,
    console: {
      log: (...args) => stdout.push(fmt(args)),
      info: (...args) => stdout.push(fmt(args)),
      warn: (...args) => stderr.push(fmt(args)),
      error: (...args) => stderr.push(fmt(args)),
      debug: (...args) => stdout.push(fmt(args)),
    },
  };
}

function stripRedeclaredPlaywrightBindings(code) {
  // 持久化会话已通过 AsyncFunction 参数注入 chromium/page/helpers。
  // 用户脚本再写 const { chromium } = require('playwright') 会 SyntaxError 并中断整步。
  return String(code || '')
    .replace(
      /(?:const|let|var)\s*\{[^}]*\}\s*=\s*require\(\s*['"]playwright['"]\s*\)\s*;?/g,
      ''
    )
    .replace(
      /(?:const|let|var)\s*\{[^}]*\}\s*=\s*require\(\s*['"]\.\/lib\/helpers['"]\s*\)\s*;?/g,
      ''
    )
    .replace(
      /^\s*(?:const|let|var)\s+(?:chromium|firefox|webkit|devices|helpers|browser|context|page)\s*=\s*[^;\n]+;?\s*$/gm,
      ''
    )
    .replace(
      /^\s*(?:const|let|var)\s+browser\s*=\s*await\s+chromium\.launch[\s\S]*?;?\s*$/gm,
      ''
    );
}

const PERSISTENT_USER_CODE_PREFIX = `
let { browser, context, page } = state;
if (page && helpers && typeof helpers.dismissBlockingDialogs === 'function') {
  try { await helpers.dismissBlockingDialogs(page); } catch (_) {}
}
if (page && String(process.env.WHARTTEST_NOTIFICATION_CASE || '') === '1') {
  const __url = String(page.url() || '');
  if (__url && !/\\/login(?:\\?|$|\\/)/.test(__url) && !/\\/notifications(?:\\?|$|\\/)/.test(__url)) {
    try {
      if (helpers && typeof helpers.navigateToNotificationRecordsPage === 'function') {
        await helpers.navigateToNotificationRecordsPage(page);
      } else {
        const __origin = new URL(__url).origin;
        await page.goto(__origin + '/work-order/notifications', { waitUntil: 'domcontentloaded' });
      }
    } catch (_) {}
  }
}
const __screenshotDir = process.env.SCREENSHOT_DIR;
if (page && __screenshotDir) {
  const __fs = require('fs');
  const __path = require('path');
  try { __fs.mkdirSync(__screenshotDir, { recursive: true }); } catch (_) {}
  if (!page.__wharttestOriginalScreenshot) {
    Object.defineProperty(page, '__wharttestOriginalScreenshot', {
      value: page.screenshot.bind(page),
      configurable: false,
      enumerable: false,
      writable: false,
    });
  }
  const __origScreenshot = page.__wharttestOriginalScreenshot;
  const __minScreenshotBytes = 15000;
  const __isBlankScreenshot = (filePath) => {
    try {
      return __fs.statSync(filePath).size < __minScreenshotBytes;
    } catch (_) {
      return true;
    }
  };
  page.screenshot = async (opts = {}) => {
    const requested = opts && opts.path;
    const basename = requested ? __path.basename(String(requested)) : 'last.png';
    const savedPath = __path.join(__screenshotDir, basename);
    const onLogin = String(page.url() || '').includes('/login');
    if (!onLogin && String(process.env.WHARTTEST_NOTIFICATION_CASE || '') === '1'
        && !String(page.url() || '').includes('/notifications')) {
      try {
        if (helpers && typeof helpers.navigateToNotificationRecordsPage === 'function') {
          await helpers.navigateToNotificationRecordsPage(page);
        }
      } catch (_) {}
    }
    if (onLogin || opts.plain === true || page.__inScreenshotCapture) {
      const result = await __origScreenshot({
        ...opts,
        path: savedPath,
        timeout: opts.timeout || 8000,
      });
      console.log('[SCREENSHOT_SAVED] ' + savedPath);
      return result;
    }
    page.__inScreenshotCapture = true;
    let result;
    try {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        if (attempt > 0) {
          try { await page.waitForTimeout(600); } catch (_) {}
        }
        if (helpers && typeof helpers.captureStepScreenshot === 'function') {
          await helpers.captureStepScreenshot(page, savedPath, { ...opts, prepare: attempt === 0, plain: true });
          result = savedPath;
        } else {
          if (opts.prepare !== false && helpers && typeof helpers.prepareStepScreenshot === 'function') {
            try { await helpers.prepareStepScreenshot(page); } catch (_) {}
          }
          result = await __origScreenshot({ ...opts, path: savedPath });
        }
        if (!__isBlankScreenshot(savedPath)) {
          break;
        }
      }
    } finally {
      page.__inScreenshotCapture = false;
    }
    console.log('[SCREENSHOT_SAVED] ' + savedPath);
    if (basename !== 'last.png' && !__isBlankScreenshot(savedPath)) {
      try {
        __fs.copyFileSync(savedPath, __path.join(__screenshotDir, 'last.png'));
      } catch (_) {
        try {
          await __origScreenshot({ path: __path.join(__screenshotDir, 'last.png') });
        } catch (__) {}
      }
    }
    return result;
  };
}
`;

const PERSISTENT_USER_CODE_SUFFIX = `
if (page && helpers && typeof helpers.dismissBlockingDialogs === 'function') {
  try { await helpers.dismissBlockingDialogs(page); } catch (_) {}
}
state.browser = browser;
state.context = context;
state.page = page;
`;

function buildPersistentUserCodeBody(code) {
  return (
    PERSISTENT_USER_CODE_PREFIX +
    stripRedeclaredPlaywrightBindings(code) +
    PERSISTENT_USER_CODE_SUFFIX
  );
}

async function safeClose(target, timeoutMs = 5000) {
  /**
   * Close a Playwright handle with a hard timeout.
   * Returns true if close finished within timeout, false on timeout/error/null.
   * Attach .catch immediately so a late close() rejection cannot become an
   * unhandledRejection after Promise.race already settled on timeout.
   */
  if (!target) return true;
  let timer;
  let timedOut = false;
  const closePromise = Promise.resolve()
    .then(() => target.close())
    .catch(() => undefined);
  try {
    await Promise.race([
      closePromise,
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          reject(new Error('close timeout'));
        }, timeoutMs);
      }),
    ]);
    return !timedOut;
  } catch (_) {
    return false;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function forceKillBrowserProcess(browser) {
  // Last resort when browser.close() hangs: kill the Chromium child process.
  try {
    const proc = typeof browser?.process === 'function' ? browser.process() : null;
    if (!proc || proc.killed) return false;
    serverLog('[forceKillBrowserProcess] killing pid=', proc.pid);
    try {
      proc.kill('SIGKILL');
    } catch (_) {
      try {
        proc.kill();
      } catch (__) {}
    }
    return true;
  } catch (e) {
    serverLog('[forceKillBrowserProcess]', e?.message || String(e));
    return false;
  }
}

async function main() {
  const cli = parseCli(process.argv.slice(2));
  if (!cli.skillDir) {
    serverLog('Usage: node playwright_persistent_server.js --skill-dir <path>');
    process.exit(2);
  }

  const skillDir = path.resolve(cli.skillDir);
  process.chdir(skillDir);

  const requireAnchor = fs.existsSync(path.join(skillDir, 'package.json'))
    ? path.join(skillDir, 'package.json')
    : path.join(skillDir, 'run.js');
  const requireFromSkill = Module.createRequire(requireAnchor);

  if (!checkPlaywrightInstalled(requireFromSkill)) {
    const installed = installPlaywright(skillDir);
    if (!installed) {
      serverLog('[playwright_persistent_server] Playwright unavailable.');
    }
  }

  let playwright = null;
  let chromium = null;
  let firefox = null;
  let webkit = null;
  let devices = null;
  let helpers = null;

  const state = {
    browser: null,
    context: null,
    page: null,
  };

  async function loadDeps() {
    if (!playwright) {
      playwright = requireFromSkill('playwright');
      chromium = playwright.chromium;
      firefox = playwright.firefox;
      webkit = playwright.webkit;
      devices = playwright.devices;
    }
    try {
      const helpersPath = requireFromSkill.resolve('./lib/helpers');
      delete require.cache[helpersPath];
      helpers = require(helpersPath);
    } catch (_) {
      if (!helpers) {
        helpers = {
          launchBrowser: async (type) => {
            const browsers = { chromium, firefox, webkit };
            return browsers[type || 'chromium'].launch({
              headless: process.env.HEADLESS !== 'false',
              args: ['--no-sandbox', '--disable-setuid-sandbox'],
            });
          },
          getExtraHeadersFromEnv: () => null,
        };
      }
    }
  }

  function getContextOptionsWithHeaders(options = {}) {
    if (!helpers?.getExtraHeadersFromEnv) return options;
    const extra = helpers.getExtraHeadersFromEnv();
    if (!extra) return options;
    return {
      ...options,
      extraHTTPHeaders: {
        ...(extra || {}),
        ...(options?.extraHTTPHeaders || {}),
      },
    };
  }

  async function resetBrowserState() {
    // Drop refs first so concurrent ensureBrowserContextPage cannot reuse them.
    // Close browser first (covers pages/contexts). Sequential page->context->browser
    // left orphans when page.close() hung past timeout and browser never closed.
    const page = state.page;
    const context = state.context;
    const browser = state.browser;
    state.page = null;
    state.context = null;
    state.browser = null;

    serverLog('[resetBrowserState] closing browser (then context/page fallback)');
    if (browser) {
      const ok = await safeClose(browser, 8000);
      if (!ok) {
        serverLog('[resetBrowserState] browser.close timed out; force-killing process');
        forceKillBrowserProcess(browser);
      }
    } else {
      // No browser handle (e.g. only context from launchPersistentContext path).
      await Promise.all([safeClose(page, 3000), safeClose(context, 5000)]);
    }
    serverLog('[resetBrowserState] done');
  }

  async function pruneUntrackedResources() {
    // User code may open extra pages/contexts that are not assigned back to state.
    try {
      if (!state.browser || (typeof state.browser.isConnected === 'function' && !state.browser.isConnected())) {
        return;
      }
      const contexts =
        typeof state.browser.contexts === 'function' ? state.browser.contexts() : [];
      const contextTasks = (contexts || []).map(async (ctx) => {
        try {
          if (state.context && ctx === state.context) {
            const pages = typeof ctx.pages === 'function' ? ctx.pages() : [];
            const pageTasks = [];
            for (const p of pages || []) {
              if (state.page && p === state.page) {
                continue;
              }
              pageTasks.push(safeClose(p));
            }
            await Promise.all(pageTasks);
            return;
          }
          await safeClose(ctx);
        } catch (e) {
          serverLog('[pruneUntrackedResources]', e?.stack || e?.message || String(e));
        }
      });
      await Promise.all(contextTasks);
    } catch (e) {
      serverLog('[pruneUntrackedResources]', e?.stack || e?.message || String(e));
    }
  }

  async function ensureBrowserContextPage() {
    await loadDeps();

    const browserMissing = !state.browser;
    const browserDisconnected =
      !!state.browser &&
      typeof state.browser.isConnected === 'function' &&
      !state.browser.isConnected();

    if (browserMissing || browserDisconnected) {
      // Close old handles before launching again to avoid orphan Chromium processes.
      await resetBrowserState();
      const browserType = (process.env.PW_BROWSER_TYPE || 'chromium').toLowerCase();
      state.browser = await helpers.launchBrowser(browserType);
    }

    if (!state.context) {
      state.context = await state.browser.newContext(getContextOptionsWithHeaders({}));
    }

    if (!state.page || (typeof state.page.isClosed === 'function' && state.page.isClosed())) {
      state.page = await state.context.newPage();
      try {
        await state.page.setViewportSize({ width: 1280, height: 1024 });
      } catch (_) {
        // ignore
      }
    }
  }

  function resolveCodeFromArgs(args) {
    const a = Array.isArray(args) ? args : [];
    if (a.length > 0) {
      const first = a[0];
      if (typeof first === 'string' && fs.existsSync(first)) {
        const filePath = path.resolve(first);
        return fs.readFileSync(filePath, 'utf8');
      }
      return a.join(' ');
    }
    return '';
  }

  async function runUserCode(code) {
    const captured = createCapturedConsole();
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

    const safeProcess = Object.assign({}, process, {
      exit: (code) => {
        throw new Error(`process.exit(${code}) blocked in persistent session`);
      },
    });

    // 调试：记录实际执行的代码
    serverLog('[runUserCode] Code length:', code.length);
    serverLog('[runUserCode] Code preview:', code.slice(0, 200));

    const body = buildPersistentUserCodeBody(code);
    let fn;
    try {
      fn = new AsyncFunction(
        'console',
        'state',
        'helpers',
        'chromium',
        'firefox',
        'webkit',
        'devices',
        'require',
        'process',
        'getContextOptionsWithHeaders',
        body
      );
    } catch (syntaxError) {
      const message = syntaxError?.message || String(syntaxError);
      serverLog('[runUserCode] SyntaxError:', message);
      serverLog('[runUserCode] Body preview:', body.slice(0, 500));
      return {
        ok: false,
        stdout: [],
        stderr: [
          `SyntaxError: ${message}`,
          '提示: 步骤1登录用 await helpers.loginStep1(page); 步骤N截图用 await helpers.screenshotCaseStep(page, N); 禁止手写路径。'
        ],
        error: message,
      };
    }

    const prepareScreenshotView = async (page) => {
      if (helpers && typeof helpers.prepareStepScreenshot === 'function') {
        try {
          await helpers.prepareStepScreenshot(page);
          return;
        } catch (_) {
          // fall through to generic scroll
        }
      }
      try {
        await page.evaluate(() => {
          window.scrollBy(0, Math.min(480, window.innerHeight * 0.55));
        });
        await page.waitForTimeout(200);
      } catch (_) {
        // ignore
      }
    };

    const saveLastScreenshot = async () => {
      const dir = process.env.SCREENSHOT_DIR;
      const page = state.page;
      if (!dir || !page || (typeof page.isClosed === 'function' && page.isClosed())) {
        return;
      }
      const stdoutText = captured.stdout.join('\n');
      const caseScreenshotLogged =
        stdoutText.includes('[CASE_SCREENSHOT]')
        || /\[SCREENSHOT_SAVED\]\s+\S*case_\d+_step\d+\.png/i.test(stdoutText);
      if (caseScreenshotLogged) {
        return;
      }
      const caseId = process.env.WHARTTEST_CASE_ID;
      if (caseId) {
        try {
          fs.mkdirSync(dir, { recursive: true });
          const requestedSteps = [
            ...String(code || '').matchAll(
              /screenshotCaseStep\s*\(\s*page\s*,\s*(\d+)/gi,
            ),
          ];
          if (requestedSteps.length === 0) {
            return;
          }
          const step = Number(requestedSteps[requestedSteps.length - 1][1]);
          const maxStep = Number(process.env.WHARTTEST_CASE_STEP_COUNT || 0) || 30;
          if (!Number.isInteger(step) || step < 1 || step > maxStep) {
            return;
          }
          if (helpers && typeof helpers.screenshotCaseStep === 'function') {
            await helpers.screenshotCaseStep(page, step, caseId);
          } else {
            const stepPath = path.join(dir, `case_${caseId}_step${step}.png`);
            if (helpers && typeof helpers.captureStepScreenshot === 'function') {
              await helpers.captureStepScreenshot(page, stepPath);
            } else {
              await page.screenshot({ path: stepPath, fullPage: false });
            }
            captured.stdout.push(`[CASE_SCREENSHOT] ${stepPath}`);
          }
        } catch (err) {
          captured.stderr.push(`[SCREENSHOT_SAVE_FAILED] ${err?.message || String(err)}`);
        }
        return;
      }
      try {
        fs.mkdirSync(dir, { recursive: true });
        state.screenshotSeq = (state.screenshotSeq || 0) + 1;
        const seq = String(state.screenshotSeq).padStart(2, '0');
        const stepFile = `step_${seq}.png`;
        const stepPath = path.join(dir, stepFile);
        const lastPath = path.join(dir, 'last.png');
        if (helpers && typeof helpers.captureStepScreenshot === 'function') {
          await helpers.captureStepScreenshot(page, stepPath);
        } else {
          await prepareScreenshotView(page);
          await page.screenshot({ path: stepPath });
        }
        try {
          fs.copyFileSync(stepPath, lastPath);
        } catch (_) {
          await page.screenshot({ path: lastPath });
        }
        captured.stdout.push(`[SCREENSHOT_SAVED] ${stepPath}`);
        captured.stdout.push(`[SCREENSHOT_SAVED] ${lastPath}`);
        captured.stdout.push(`[SCREENSHOT_STEP_FILE] ${stepFile}`);
      } catch (err) {
        captured.stderr.push(`[SCREENSHOT_SAVE_FAILED] ${err?.message || String(err)}`);
      }
    };

    const originalGlobalConsole = global.console;
    global.console = captured.console;
    try {
      await fn(
        captured.console,
        state,
        helpers,
        chromium,
        firefox,
        webkit,
        devices,
        requireFromSkill,
        safeProcess,
        getContextOptionsWithHeaders
      );
      await saveLastScreenshot();
      return { ok: true, stdout: captured.stdout, stderr: captured.stderr };
    } catch (e) {
      const msg = e?.stack || e?.message || String(e);
      captured.stderr.push(msg);
      await saveLastScreenshot();
      return { ok: false, stdout: captured.stdout, stderr: captured.stderr, error: msg };
    } finally {
      global.console = originalGlobalConsole;
    }
  }

  let chain = Promise.resolve();
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

  rl.on('line', (line) => {
    const raw = (line || '').trim();
    if (!raw) return;

    chain = chain.then(async () => {
      let req;
      try {
        req = JSON.parse(raw);
      } catch (_) {
        // 无效 JSON，忽略
        return;
      }

      // 验证请求格式：必须是对象且包含 id
      if (!req || typeof req !== 'object' || Array.isArray(req) || !req.id) {
        // 畸形请求，忽略（无法响应因为没有有效 id）
        serverLog('[playwright_persistent_server] Malformed request ignored:', raw.slice(0, 100));
        return;
      }

      const id = req.id;
      const method = req.method;
      const params = req.params || {};

      // 仅处理允许的 env 变量，过滤敏感变量
      const BLOCKED_ENV_KEYS = ['NODE_OPTIONS', 'PATH', 'HOME', 'USERPROFILE', 'TEMP', 'TMP'];
      if (params.env && typeof params.env === 'object') {
        for (const [k, v] of Object.entries(params.env)) {
          if (BLOCKED_ENV_KEYS.includes(k.toUpperCase())) {
            continue; // 跳过敏感变量
          }
          if (v === null || v === undefined) {
            delete process.env[k];
          } else {
            process.env[k] = String(v);
          }
        }
      }

      try {
        if (method === 'ping') {
          send({ id, ok: true, state: { alive: true } });
          return;
        }

        if (method === 'close') {
          await resetBrowserState();
          send({ id, ok: true });
          setTimeout(() => process.exit(0), 10);
          return;
        }

        if (method === 'exec') {
          await ensureBrowserContextPage();
          const code = resolveCodeFromArgs(params.args);
          if (!code) {
            send({
              id,
              ok: false,
              error: 'No code to execute (args empty)',
              stdout: [],
              stderr: [],
            });
            return;
          }

          const execTimeoutMs = Number(process.env.PW_EXEC_TIMEOUT_MS || 45000);
          let timer;
          let result;
          try {
            result = await Promise.race([
              runUserCode(code),
              new Promise((_, reject) => {
                timer = setTimeout(() => {
                  reject(new Error(`Playwright exec timed out after ${execTimeoutMs}ms`));
                }, execTimeoutMs);
              }),
            ]);
          } catch (e) {
            const msg = e?.message || String(e);
            if (String(msg).includes('timed out')) {
              serverLog('[exec] hard timeout, resetting browser');
              await resetBrowserState().catch(() => {});
              send({
                id,
                ok: false,
                error: msg,
                stdout: [],
                stderr: [
                  msg,
                  '浏览器已重置。请先重新打开该步骤所在页面（登录/菜单/按钮），再做断言和截图，不要只重复同一段 waitFor。',
                ],
              });
              return;
            }
            send({
              id,
              ok: false,
              error: msg,
              stdout: [],
              stderr: [msg],
            });
            return;
          } finally {
            if (timer) clearTimeout(timer);
          }
          await pruneUntrackedResources();
          const pageUrl = state.page && typeof state.page.url === 'function' ? state.page.url() : null;
          send({
            id,
            ok: !!result.ok,
            stdout: result.stdout || [],
            stderr: result.stderr || [],
            error: result.error,
            state: { pageUrl },
          });
          return;
        }

        send({ id, ok: false, error: `Unknown method: ${method}`, stdout: [], stderr: [] });
      } catch (e) {
        const msg = e?.stack || e?.message || String(e);
        send({ id, ok: false, error: msg, stdout: [], stderr: [msg] });
      }
    });
  });

  serverLog('[playwright_persistent_server] Ready. Waiting for commands...');
}

main().catch((e) => {
  serverLog('[playwright_persistent_server] fatal:', e?.stack || String(e));
  process.exit(1);
});
