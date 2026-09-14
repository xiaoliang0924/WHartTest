// Playwright 辅助函数模块
// Playwright 自动化可复用工具函数

const { chromium, firefox, webkit } = require('playwright');

/**
 * 从环境变量解析额外 HTTP 请求头。
 * 支持两种格式：
 * - PW_HEADER_NAME + PW_HEADER_VALUE：单个请求头（简单常用）
 * - PW_EXTRA_HEADERS：多个请求头的 JSON 对象（高级用法）
 * 若两种同时存在，优先使用单请求头格式。
 * @returns {Object|null} 请求头对象；若未配置则返回 null
 */
function getExtraHeadersFromEnv() {
  const headerName = process.env.PW_HEADER_NAME;
  const headerValue = process.env.PW_HEADER_VALUE;

  if (headerName && headerValue) {
    return { [headerName]: headerValue };
  }

  const headersJson = process.env.PW_EXTRA_HEADERS;
  if (headersJson) {
    try {
      const parsed = JSON.parse(headersJson);
      if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
        return parsed;
      }
      console.warn('PW_EXTRA_HEADERS must be a JSON object, ignoring...');
    } catch (e) {
      console.warn('Failed to parse PW_EXTRA_HEADERS as JSON:', e.message);
    }
  }

  return null;
}

/**
 * 以标准配置启动浏览器
 * @param {string} browserType - 浏览器类型：'chromium'、'firefox' 或 'webkit'
 * @param {Object} options - 额外启动配置
 */
async function launchBrowser(browserType = 'chromium', options = {}) {
  const defaultOptions = {
    headless: process.env.HEADLESS !== 'false',
    slowMo: process.env.SLOW_MO ? parseInt(process.env.SLOW_MO) : 0,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  };
  
  const browsers = { chromium, firefox, webkit };
  const browser = browsers[browserType];
  
  if (!browser) {
    throw new Error(`Invalid browser type: ${browserType}`);
  }
  
  return await browser.launch({ ...defaultOptions, ...options });
}

/**
 * 创建新页面并应用视口与 UA 配置
 * @param {Object} context - 浏览器上下文
 * @param {Object} options - 页面配置
 */
async function createPage(context, options = {}) {
  const page = await context.newPage();
  
  if (options.viewport) {
    await page.setViewportSize(options.viewport);
  }
  
  if (options.userAgent) {
    await page.setExtraHTTPHeaders({
      'User-Agent': options.userAgent
    });
  }
  
  // 设置默认超时时间
  page.setDefaultTimeout(options.timeout || 30000);
  
  return page;
}

/**
 * 智能等待页面就绪
 * @param {Object} page - Playwright 页面对象
 * @param {Object} options - 等待配置
 */
async function waitForPageReady(page, options = {}) {
  const waitOptions = {
    waitUntil: options.waitUntil || 'networkidle',
    timeout: options.timeout || 30000
  };
  
  try {
    await page.waitForLoadState(waitOptions.waitUntil, { 
      timeout: waitOptions.timeout 
    });
  } catch (e) {
    console.warn('Page load timeout, continuing...');
  }
  
  // 若提供了选择器，则额外等待动态内容出现
  if (options.waitForSelector) {
    await page.waitForSelector(options.waitForSelector, { 
      timeout: options.timeout 
    });
  }
}

/**
 * 带重试机制的安全点击
 * @param {Object} page - Playwright 页面对象
 * @param {string} selector - 元素选择器
 * @param {Object} options - 点击配置
 */
async function safeClick(page, selector, options = {}) {
  const maxRetries = options.retries || 3;
  const retryDelay = options.retryDelay || 1000;
  
  for (let i = 0; i < maxRetries; i++) {
    try {
      await page.waitForSelector(selector, { 
        state: 'visible',
        timeout: options.timeout || 5000 
      });
      await page.click(selector, {
        force: options.force || false,
        timeout: options.timeout || 5000
      });
      return true;
    } catch (e) {
      if (i === maxRetries - 1) {
        console.error(`Failed to click ${selector} after ${maxRetries} attempts`);
        throw e;
      }
      console.log(`Retry ${i + 1}/${maxRetries} for clicking ${selector}`);
      await page.waitForTimeout(retryDelay);
    }
  }
}

/**
 * 安全文本输入（输入前可先清空）
 * @param {Object} page - Playwright 页面对象
 * @param {string} selector - 输入框选择器
 * @param {string} text - 要输入的文本
 * @param {Object} options - 输入配置
 */
async function safeType(page, selector, text, options = {}) {
  await page.waitForSelector(selector, { 
    state: 'visible',
    timeout: options.timeout || 10000 
  });
  
  if (options.clear !== false) {
    await page.fill(selector, '');
  }
  
  if (options.slow) {
    await page.type(selector, text, { delay: options.delay || 100 });
  } else {
    await page.fill(selector, text);
  }
}

/**
 * 批量提取多个元素文本
 * @param {Object} page - Playwright 页面对象
 * @param {string} selector - 元素选择器
 */
async function extractTexts(page, selector) {
  await page.waitForSelector(selector, { timeout: 10000 });
  return await page.$$eval(selector, elements => 
    elements.map(el => el.textContent?.trim()).filter(Boolean)
  );
}

const MAIN_CONTENT_PANEL_SELECTORS = [
  '.layout-content',
  '.el-main',
  '.main-container',
  '.page-container',
];

const MIN_SCREENSHOT_BYTES = 15000;

function isTinyScreenshot(filePath) {
  const fs = require('fs');
  try {
    return !fs.existsSync(filePath) || fs.statSync(filePath).size < MIN_SCREENSHOT_BYTES;
  } catch (_) {
    return true;
  }
}

async function locatorHasVisibleContent(locator) {
  try {
    const text = String(await locator.innerText({ timeout: 1500 })).replace(/\s+/g, '');
    if (text.length >= 24) {
      return true;
    }
    const widgets = locator.locator(
      'table, button, input, img, canvas, .el-table, .el-button, .el-form, .el-pagination',
    );
    if ((await widgets.count()) === 0) {
      return false;
    }
    return widgets.first().isVisible().catch(() => false);
  } catch (_) {
    return false;
  }
}

async function screenshotJpegBytes(page, clip) {
  try {
    const opts = {
      type: 'jpeg',
      quality: 30,
      fullPage: false,
      timeout: 4000,
      plain: true,
    };
    if (clip && clip.width > 10 && clip.height > 10) {
      opts.clip = {
        x: Math.max(0, clip.x),
        y: Math.max(0, clip.y),
        width: clip.width,
        height: clip.height,
      };
    }
    const buf = await page.screenshot(opts);
    return Buffer.isBuffer(buf) ? buf.length : 0;
  } catch (_) {
    return 0;
  }
}

async function regionLooksBlank(page, clip) {
  const bytes = await screenshotJpegBytes(page, clip);
  return bytes > 0 && bytes < 6000;
}

/**
 * 将主内容区滚回顶部，使搜索框/筛选区与列表头部同屏可见。
 */
async function scrollMainContentToTop(page) {
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return;
  }

  try {
    await page.setViewportSize({ width: 1280, height: 1024 });
  } catch (_) {
    // ignore
  }

  await page.evaluate((selectors) => {
    window.scrollTo(0, 0);
    for (const selector of selectors) {
      const panel = document.querySelector(selector);
      if (!panel) {
        continue;
      }
      const scrollEl =
        panel.querySelector('.el-scrollbar__wrap') ||
        panel.closest('.el-scrollbar__wrap') ||
        panel;
      if (scrollEl && scrollEl.scrollTop !== undefined) {
        scrollEl.scrollTop = 0;
      }
    }
    const wraps = document.querySelectorAll(
      '.layout-content .el-scrollbar__wrap, .el-main .el-scrollbar__wrap',
    );
    wraps.forEach((el) => {
      el.scrollTop = 0;
    });
  }, MAIN_CONTENT_PANEL_SELECTORS);

  await page.waitForTimeout(300);
}

/**
 * 将列表表格滚入主内容视口（Element Plus 布局用内部 el-scrollbar 滚动）。
 */
async function scrollMainContentToTable(page) {
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return;
  }

  try {
    await page.setViewportSize({ width: 1280, height: 1024 });
  } catch (_) {
    // ignore
  }

  await page.evaluate(() => {
    const scrollEl =
      document.querySelector('.layout-content .el-scrollbar__wrap') ||
      document.querySelector('.el-main .el-scrollbar__wrap') ||
      document.querySelector('.el-scrollbar__wrap:not(.el-select-dropdown__wrap)') ||
      document.scrollingElement;

    const hostRect = scrollEl && scrollEl.getBoundingClientRect
      ? scrollEl.getBoundingClientRect()
      : { top: 0 };

    const scrollByOffset = (delta) => {
      if (!scrollEl) {
        window.scrollBy(0, delta);
        return;
      }
      if (scrollEl === document.scrollingElement) {
        window.scrollBy(0, delta);
      } else {
        scrollEl.scrollTop = Math.max(0, scrollEl.scrollTop + delta);
      }
    };

    const table =
      document.querySelector('.layout-content .el-table') ||
      document.querySelector('.el-main .el-table') ||
      document.querySelector('.el-table');
    if (table) {
      const tableRect = table.getBoundingClientRect();
      const targetTop = hostRect.top + 48;
      scrollByOffset(tableRect.top - targetTop);
      // 再滚一点，让表头+首行数据都可见
      scrollByOffset(80);
      return;
    }

    // 无表格时：滚过「筛选条件」卡片
    const filterTitle = Array.from(document.querySelectorAll('*')).find(
      (el) => el.childElementCount < 8 && /^筛选条件$/.test((el.textContent || '').trim()),
    );
    if (filterTitle) {
      const card = filterTitle.closest('.el-card, section, div');
      const bottom = (card || filterTitle).getBoundingClientRect().bottom;
      scrollByOffset(bottom - hostRect.top - 40);
      return;
    }

    scrollByOffset(560);
  });

  await page.waitForTimeout(400);
}

/**
 * 截图前将主内容滚回顶部，使搜索/筛选与列表同屏。
 * @param {Object} page - Playwright 页面对象
 */
async function prepareStepScreenshot(page) {
  await scrollMainContentToTop(page);
}

async function resolveScreenshotClip(page) {
  await scrollMainContentToTop(page);

  return page.evaluate((selectors) => {
    const toClip = (rect, minHeight = 180) => {
      if (!rect || rect.width < 200 || rect.height < 40) {
        return null;
      }
      const x = Math.max(0, Math.floor(rect.x));
      const y = Math.max(0, Math.floor(rect.y));
      const width = Math.min(Math.floor(rect.width), window.innerWidth - x);
      const height = Math.min(
        Math.floor(Math.max(rect.height, minHeight)),
        Math.min(900, window.innerHeight - y),
      );
      if (width < 120 || height < 80) {
        return null;
      }
      return { x, y, width, height };
    };

    for (const selector of selectors) {
      const panel = document.querySelector(selector);
      if (!panel) {
        continue;
      }
      const clip = toClip(panel.getBoundingClientRect(), 420);
      if (clip) {
        return clip;
      }
    }

    const searchForm =
      document.querySelector('.layout-content .el-form') ||
      document.querySelector('.el-main .el-form') ||
      document.querySelector('.search-form, .filter-container, .el-card');
    if (searchForm) {
      searchForm.scrollIntoView({ block: 'start', inline: 'nearest' });
      const top = Math.max(0, Math.floor(searchForm.getBoundingClientRect().top - 8));
      return {
        x: 0,
        y: top,
        width: window.innerWidth,
        height: Math.min(900, window.innerHeight - top - 8),
      };
    }

    return {
      x: 0,
      y: 0,
      width: window.innerWidth,
      height: Math.min(900, window.innerHeight),
    };
  }, MAIN_CONTENT_PANEL_SELECTORS);
}

/**
 * 截取主内容区（搜索/筛选 + 列表），避免只截表格 DOM 而丢失搜索框。
 */
async function captureMainContentView(page, targetPath, options = {}) {
  const fs = require('fs');
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return false;
  }

  await dismissBlockingDialogs(page);
  await scrollMainContentToTop(page);
  await page.waitForTimeout(300);

  for (const selector of MAIN_CONTENT_PANEL_SELECTORS) {
    const panel = page.locator(selector).first();
    try {
      if ((await panel.count()) === 0) {
        continue;
      }
      if (!await locatorHasVisibleContent(panel)) {
        continue;
      }
      const box = await panel.boundingBox();
      if (box && await regionLooksBlank(page, box)) {
        continue;
      }
      await panel.screenshot({ ...options, path: targetPath, timeout: 2500 });
      if (!isTinyScreenshot(targetPath)) {
        return true;
      }
    } catch (_) {
      // 面板被遮罩/未稳定时不要拖满默认 15s，改试下一个
    }
  }

  try {
    const clip = await resolveScreenshotClip(page);
    if (clip && clip.width > 50 && clip.height > 50 && !await regionLooksBlank(page, clip)) {
      await page.screenshot({
        ...options,
        path: targetPath,
        clip,
        fullPage: false,
        timeout: 8000,
        plain: true,
      });
      if (fs.existsSync(targetPath) && fs.statSync(targetPath).size >= 8000) {
        return true;
      }
    }
  } catch (_) {
    // fall through
  }

  await page.screenshot({
    ...options,
    path: targetPath,
    fullPage: false,
    timeout: 8000,
    plain: true,
  });
  if (await regionLooksBlank(page)) {
    console.log(`RESULT=FAIL: 截图为空或仍是白屏，当前 URL=${page.url()}`);
    return false;
  }
  return fs.existsSync(targetPath);
}

async function captureTicketListView(page, targetPath, options = {}) {
  return captureMainContentView(page, targetPath, options);
}

function isOverviewCaseContext() {
  return String(process.env.WHARTTEST_OVERVIEW_CASE || '').trim() === '1';
}

function isOverviewSlaDetailCaseContext() {
  return String(process.env.WHARTTEST_OVERVIEW_SLA_DETAIL_CASE || '').trim() === '1';
}

let lastClickedSlaTicketNo = '';

function getClickedSlaTicketNo() {
  return String(process.env.WHARTTEST_CLICKED_TICKET_NO || lastClickedSlaTicketNo || '').trim();
}

function setClickedSlaTicketNo(value) {
  lastClickedSlaTicketNo = String(value || '').trim();
  if (lastClickedSlaTicketNo) {
    process.env.WHARTTEST_CLICKED_TICKET_NO = lastClickedSlaTicketNo;
  }
}

function isDashboardUrl(url) {
  return /\/work-order\/dashboard(?:\?|$|\/)/.test(String(url || ''));
}

/**
 * 数据总览页截图：优先整页 dashboard-content（含 KPI / 图表头部）。
 */
async function captureDashboardView(page, targetPath, options = {}) {
  const fs = require('fs');
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return false;
  }

  await dismissBlockingDialogs(page);
  await scrollMainContentToTop(page);
  await page.waitForTimeout(400);

  const dashboard = page.locator('.dashboard-content').first();
  try {
    if ((await dashboard.count()) > 0) {
      await dashboard.waitFor({ state: 'visible', timeout: 10000 });
      await dashboard.screenshot({ path: targetPath, ...options });
      if (fs.existsSync(targetPath) && fs.statSync(targetPath).size >= 8000) {
        return true;
      }
    }
  } catch (_) {
    // fall through
  }

  const header = page.locator('.page-header, .page-header-content').first();
  try {
    if ((await header.count()) > 0) {
      await header.waitFor({ state: 'visible', timeout: 8000 });
      const clip = await header.boundingBox();
      if (clip && clip.width > 100 && clip.height > 80) {
        await page.screenshot({
          path: targetPath,
          clip: {
            x: Math.max(0, clip.x - 8),
            y: Math.max(0, clip.y - 8),
            width: Math.min(clip.width + 16, 1280),
            height: Math.min(Math.max(clip.height + 400, 520), 900),
          },
          fullPage: false,
          timeout: 8000,
          ...options,
        });
        if (fs.existsSync(targetPath) && fs.statSync(targetPath).size >= 8000) {
          return true;
        }
      }
    }
  } catch (_) {
    // fall through
  }

  return captureMainContentView(page, targetPath, options);
}

/**
 * 按步骤截取数据总览页不同区域，避免多步截图完全相同。
 */
async function captureDashboardStepView(page, targetPath, stepNumber, options = {}) {
  const fs = require('fs');
  const step = Number(stepNumber);
  await dismissBlockingDialogs(page);
  await scrollMainContentToTop(page);
  await page.waitForTimeout(300);

  const shotLocator = async (locator) => {
    if ((await locator.count()) === 0) {
      return false;
    }
    await locator.first().waitFor({ state: 'visible', timeout: 10000 });
    await locator.first().screenshot({ path: targetPath, ...options });
    return fs.existsSync(targetPath) && fs.statSync(targetPath).size >= 8000;
  };

  if (step === 2) {
    if (await shotLocator(page.locator('.page-header'))) {
      return true;
    }
  }

  if (step === 3) {
    const kpiBlock = page.locator('.dashboard-content').filter({ hasText: /近7天.*工单数|新建工单/ }).first();
    if (await shotLocator(kpiBlock)) {
      return true;
    }
  }

  if (step === 4) {
    const chart = page.locator('.echarts-for-react, canvas').first();
    if (await shotLocator(chart)) {
      return true;
    }
  }

  if (step === 5) {
    const chart = page.locator('.echarts-for-react, canvas').nth(1);
    if (await shotLocator(chart)) {
      return true;
    }
  }

  if (step === 6) {
    const table = page.locator('.dashboard-content').filter({ hasText: /处理人效率排行|处理人/ }).first();
    if (await shotLocator(table)) {
      return true;
    }
  }

  if (step === 7) {
    const sla = page.locator('.dashboard-content').filter({ hasText: /SLA|预警/ }).first();
    if (await shotLocator(sla)) {
      return true;
    }
  }

  return captureDashboardView(page, targetPath, options);
}

async function assertOverviewKpiCards(page) {
  const patterns = [
    /新建工单|新增工单/,
    /待处理/,
    /处理中/,
    /处理工单|已完工/,
    /关闭工单|关单/,
    /超时工单|超时/,
    /超时率|及时率/,
  ];
  for (const pattern of patterns) {
    try {
      await page.getByText(pattern).first().waitFor({ state: 'visible', timeout: 10000 });
    } catch (_) {
      console.log(`RESULT=FAIL: KPI 卡片未找到，匹配 ${pattern}`);
      return false;
    }
  }
  return true;
}

async function ensureOverviewPageForScreenshot(page, stepNumber) {
  if (!isOverviewCaseContext() || Number(stepNumber) < 2) {
    return true;
  }
  if (isDashboardUrl(page.url())) {
    return true;
  }
  console.log(
    `步骤${stepNumber}截图前不在数据总览页，当前 URL=${page.url()}，尝试自动导航…`,
  );
  await navigateByMenuPath(
    page,
    ['工单中心', '工单总览'],
    /\/work-order\/dashboard/,
    '/work-order/dashboard',
  );
  await page.waitForLoadState('networkidle').catch(() => {});
  await dismissBlockingDialogs(page);
  if (!isDashboardUrl(page.url())) {
    console.log(
      `RESULT=FAIL: 步骤${stepNumber}截图失败，未能进入数据总览页，当前 URL=${page.url()}`,
    );
    return false;
  }
  await waitForPageBodyText(page, 120, 15000);
  return true;
}

/**
 * 工单总览用例专用：导航 + 校验 + 截图 + 输出 RESULT=PASS。每步只调用这一行。
 */
async function runOverviewCaseStep(page, stepNumber, caseId) {
  const step = Number(stepNumber);
  const cid = caseId || process.env.WHARTTEST_CASE_ID || 'unknown';

  if (step === 1) {
    const path = await loginStep1(page, cid);
    if (!isLoginPageUrl(page.url())) {
      console.log(`RESULT=PASS: 步骤1登录成功 URL=${page.url()}`);
    }
    return path;
  }

  const ready = await ensureOverviewPageForScreenshot(page, step);
  if (!ready) {
    await screenshotCaseStep(page, step, cid);
    return null;
  }

  if (step === 2) {
    const ok = await assertOverviewPageLoaded(page);
    if (!ok) {
      await screenshotCaseStep(page, step, cid);
      return null;
    }
  } else if (step === 3) {
    const ok = await assertOverviewKpiCards(page);
    if (!ok) {
      await screenshotCaseStep(page, step, cid);
      return null;
    }
  }

  await waitForPageBodyText(page, 120, 15000);
  const pathMod = require('path');
  const dir = process.env.SCREENSHOT_DIR || '.';
  const target = pathMod.join(dir, `case_${cid}_step${step}.png`);
  await captureDashboardStepView(page, target, step);
  console.log('[CASE_SCREENSHOT]', target);
  console.log(`RESULT=PASS: 步骤${step}完成 URL=${page.url()}`);
  return target;
}

function getSlaWarningSectionLocator(page) {
  return page
    .locator('.dashboard-content, .layout-content, main')
    .filter({ hasText: /SLA预警明细|SLA预警|预警明细/ })
    .last();
}

async function scrollToSlaWarningSection(page) {
  await dismissBlockingDialogs(page);
  const section = getSlaWarningSectionLocator(page);
  try {
    if ((await section.count()) > 0) {
      await section.scrollIntoViewIfNeeded({ timeout: 10000 });
    }
  } catch (_) {
    // fall through
  }
  await page.evaluate(() => {
    const anchor = Array.from(document.querySelectorAll('h3,h4,div,span')).find((node) => {
      const text = (node.textContent || '').trim();
      return /SLA预警明细|SLA预警/.test(text) && text.length < 30;
    });
    if (anchor) {
      anchor.scrollIntoView({ block: 'center', inline: 'nearest' });
    }
    const main = document.querySelector('.layout-content, .el-main, main');
    if (main) {
      main.scrollTop = main.scrollHeight;
    }
  });
  await page.waitForTimeout(1000);
}

async function findSlaTicketRows(page) {
  await scrollToSlaWarningSection(page);
  const rowSelectors = [
    '.el-table__body tr',
    '.cl-table tbody tr',
    'table tbody tr',
    '.el-table .el-table__row',
  ];

  const title = page.getByText(/SLA预警明细|SLA预警/, { exact: false }).first();
  if ((await title.count()) > 0) {
    const scoped = title.locator(
      'xpath=ancestor::*[.//table or .//*[contains(@class,"el-table") or contains(@class,"cl-table")]][1]',
    );
    if ((await scoped.count()) > 0) {
      for (const sel of rowSelectors) {
        const rows = scoped.locator(sel);
        if ((await rows.count()) > 0) {
          return rows;
        }
      }
    }
  }

  const section = getSlaWarningSectionLocator(page);
  for (const sel of rowSelectors) {
    if ((await section.count()) > 0) {
      const rows = section.locator(sel);
      if ((await rows.count()) > 0) {
        return rows;
      }
    }
  }

  return page.locator(
    '.dashboard-content .cl-table tbody tr, .dashboard-content .el-table__body tr, .dashboard-content table tbody tr',
  );
}

async function findTicketNoLinkInRow(row) {
  const linkSelectors = ['a.tl-link', 'a.el-link', 'a[href*="/tickets/"]', 'td a', 'a'];
  for (const sel of linkSelectors) {
    const links = row.locator(sel);
    const count = await links.count();
    for (let i = 0; i < count; i += 1) {
      const link = links.nth(i);
      const text = String((await link.textContent()) || '').replace(/\s/g, '');
      if (/^20\d{12,}$/.test(text) || /^\d{12,}$/.test(text)) {
        return link;
      }
    }
  }
  const fallback = row.getByRole('link').first();
  if ((await fallback.count()) > 0) {
    return fallback;
  }
  return null;
}

async function assertSlaWarningTableHasRows(page) {
  const rows = await findSlaTicketRows(page);
  const count = await rows.count();
  if (count > 0) {
    return true;
  }
  console.log(
    'RESULT=FAIL: SLA预警明细表无数据行，请确认测试环境有近7天 SLA 超时/预警工单',
  );
  return false;
}

async function ensureOverviewDashboardPage(page) {
  if (isDashboardUrl(page.url())) {
    return assertOverviewPageLoaded(page);
  }
  await navigateToOverviewPage(page);
  await page.waitForLoadState('networkidle').catch(() => {});
  return assertOverviewPageLoaded(page);
}

async function clickFirstSlaTicketLink(page) {
  if (!(await assertSlaWarningTableHasRows(page))) {
    return '';
  }

  const rows = await findSlaTicketRows(page);
  const firstRow = rows.first();
  await firstRow.waitFor({ state: 'visible', timeout: 15000 });

  const ticketLink = await findTicketNoLinkInRow(firstRow);
  if (!ticketLink) {
    console.log('RESULT=FAIL: SLA 预警明细第一行未找到蓝色工单号链接');
    return '';
  }
  await ticketLink.waitFor({ state: 'visible', timeout: 10000 });
  const ticketNo = String((await ticketLink.textContent()) || '').trim();
  if (!ticketNo) {
    console.log('RESULT=FAIL: SLA 预警明细第一行未读取到工单号链接文本');
    return '';
  }

  await Promise.all([
    page.waitForURL(/\/work-order\/tickets\/\d+(?:\?|$|\/)/, { timeout: 20000 }).catch(() => null),
    ticketLink.click(),
  ]);
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  await dismissBlockingDialogs(page);

  if (!(await waitForTicketDetailNavigation(page, 20000))) {
    console.log(`RESULT=FAIL: 点击 SLA 工单号后未进入详情页 ticketNo=${ticketNo} URL=${page.url()}`);
    return '';
  }

  setClickedSlaTicketNo(ticketNo);
  console.log(`RESULT=PASS: 已点击 SLA 工单号 ${ticketNo} 并进入详情 URL=${page.url()}`);
  return ticketNo;
}

async function assertTicketNoOnDetailPage(page, ticketNo) {
  const no = String(ticketNo || getClickedSlaTicketNo()).trim();
  if (!no) {
    console.log('RESULT=FAIL: 缺少点击时的工单号，无法校验详情页');
    return false;
  }
  if (!(await waitForTicketDetailNavigation(page, 15000))) {
    console.log(`RESULT=FAIL: 详情页未就绪 URL=${page.url()}`);
    return false;
  }

  const bodyText = await page.locator('body').innerText().catch(() => '');
  if (bodyText.includes(no)) {
    console.log(`RESULT=PASS: 详情页已展示工单号 ${no}`);
    return true;
  }

  try {
    await page.getByText(no, { exact: false }).first().waitFor({ state: 'visible', timeout: 8000 });
    console.log(`RESULT=PASS: 详情页已展示工单号 ${no}`);
    return true;
  } catch (_) {
    console.log(
      `RESULT=FAIL: 详情页未找到工单号 ${no}，也未找到「基本信息/返回列表」等详情页标志 URL=${page.url()}`,
    );
    return false;
  }
}

async function navigateBackToOverviewFromDetail(page) {
  if (isTicketDetailUrl(page.url())) {
    try {
      await page.getByRole('button', { name: /返回列表|返回/ }).first().click({ timeout: 8000 });
      await page.waitForLoadState('domcontentloaded').catch(() => {});
    } catch (_) {
      // fall through
    }
  }
  if (!isDashboardUrl(page.url())) {
    await navigateToOverviewPage(page);
  }
  await page.waitForLoadState('networkidle').catch(() => {});
  return ensureOverviewDashboardPage(page);
}

async function clickSlaRowDetailButton(page, rowIndex = 0) {
  if (!(await assertSlaWarningTableHasRows(page))) {
    return false;
  }
  const rows = await findSlaTicketRows(page);
  const row = rows.nth(rowIndex);
  await row.waitFor({ state: 'visible', timeout: 15000 });

  const detailBtn = row.getByRole('button', { name: '详情' });
  if ((await detailBtn.count()) > 0) {
    await detailBtn.first().click();
  } else {
    const detailText = row.getByText('详情', { exact: true });
    if ((await detailText.count()) > 0) {
      await detailText.first().click();
    } else {
      const ticketLink = row.getByRole('link').first();
      if ((await ticketLink.count()) === 0) {
        console.log('RESULT=FAIL: SLA 行未找到「详情」按钮或可点击工单号');
        return false;
      }
      const ticketNo = String((await ticketLink.textContent()) || '').trim();
      await ticketLink.click();
      setClickedSlaTicketNo(ticketNo);
    }
  }

  await page.waitForLoadState('domcontentloaded').catch(() => {});
  if (!(await waitForTicketDetailNavigation(page, 20000))) {
    console.log(`RESULT=FAIL: 点击详情后未进入工单详情页 URL=${page.url()}`);
    return false;
  }
  console.log(`RESULT=PASS: 已通过详情入口进入工单详情 URL=${page.url()}`);
  return true;
}

/**
 * 工单总览 SLA 预警表 → 工单详情类用例专用。
 */
async function runOverviewSlaDetailCaseStep(page, stepNumber, caseId) {
  const step = Number(stepNumber);
  const cid = caseId || process.env.WHARTTEST_CASE_ID || 'unknown';
  const pathMod = require('path');
  const dir = process.env.SCREENSHOT_DIR || '.';
  const target = pathMod.join(dir, `case_${cid}_step${step}.png`);

  if (step === 1) {
    const path = await loginStep1(page, cid);
    if (!isLoginPageUrl(page.url())) {
      console.log(`RESULT=PASS: 步骤1登录成功 URL=${page.url()}`);
    }
    return path;
  }

  if (step === 2) {
    const ok = await ensureOverviewDashboardPage(page);
    await scrollToSlaWarningSection(page);
    await captureDashboardStepView(page, target, 7);
    console.log('[CASE_SCREENSHOT]', target);
    if (!ok) {
      return null;
    }
    console.log(`RESULT=PASS: 步骤2已进入数据总览 URL=${page.url()}`);
    return target;
  }

  if (step === 3) {
    const onDashboard = await ensureOverviewDashboardPage(page);
    if (!onDashboard) {
      await page.screenshot({ path: target, fullPage: false, timeout: 8000 });
      console.log('[CASE_SCREENSHOT]', target);
      return null;
    }
    const ticketNo = await clickFirstSlaTicketLink(page);
    await captureTicketDetailView(page, target, 3);
    console.log('[CASE_SCREENSHOT]', target);
    return ticketNo ? target : null;
  }

  if (step === 4) {
    if (!isTicketDetailUrl(page.url())) {
      const ticketNo = getClickedSlaTicketNo();
      if (!ticketNo || !(await clickFirstSlaTicketLink(page))) {
        await captureTicketDetailView(page, target, 4);
        console.log('[CASE_SCREENSHOT]', target);
        return null;
      }
    }
    const ok = await assertTicketNoOnDetailPage(page, getClickedSlaTicketNo());
    await captureTicketDetailView(page, target, 4);
    console.log('[CASE_SCREENSHOT]', target);
    if (ok) {
      console.log(`RESULT=PASS: 步骤4详情页工单号校验通过 URL=${page.url()}`);
    }
    return ok ? target : null;
  }

  if (step >= 5) {
    const backOk = await navigateBackToOverviewFromDetail(page);
    if (!backOk) {
      await page.screenshot({ path: target, fullPage: false, timeout: 8000 });
      console.log('[CASE_SCREENSHOT]', target);
      return null;
    }
    const clicked = await clickSlaRowDetailButton(page, 0);
    await captureTicketDetailView(page, target, step);
    console.log('[CASE_SCREENSHOT]', target);
    if (clicked) {
      console.log(`RESULT=PASS: 步骤${step}已通过详情按钮进入工单详情 URL=${page.url()}`);
    }
    return clicked ? target : null;
  }

  return screenshotCaseStep(page, step, cid);
}

/**
 * 步骤截图：优先主内容区（含搜索/筛选 + 列表），登录页用整页视口。
 */
async function captureStepScreenshot(page, targetPath, options = {}) {
  const fs = require('fs');
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return false;
  }

  if (isLoginPageUrl(page.url())) {
    await page.screenshot({ path: targetPath, fullPage: false, timeout: 8000, ...options });
    return fs.existsSync(targetPath);
  }

  if (isNotificationCaseContext() && !isNotificationRecordsUrl(page.url())) {
    const ready = await ensureNotificationPageForScreenshot(page, 2);
    if (!ready) {
      console.log(`RESULT=FAIL: 截图仍停在错误页面 URL=${page.url()}`);
    }
  }

  if (isNotificationRecordsUrl(page.url())) {
    return captureNotificationStepView(page, targetPath, 2, options);
  }

  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (attempt > 0) {
      await page.waitForTimeout(400);
    }
    if (await captureMainContentView(page, targetPath, options)) {
      return true;
    }
  }

  await page.screenshot({
    ...options,
    path: targetPath,
    fullPage: false,
    timeout: 8000,
    plain: true,
  });
  return fs.existsSync(targetPath);
}

function isLoginPageUrl(url) {
  return /\/login(?:\?|$|\/)/.test(String(url || ''));
}

async function hasLoginErrorBanner(page) {
  try {
    const patterns = [/rate limit/i, /重试/, /登录失败/, /密码错误/, /账号或密码/];
    for (const pattern of patterns) {
      if ((await page.getByText(pattern).count()) > 0) {
        return true;
      }
    }
  } catch (_) {
    // ignore
  }
  return false;
}

async function getLoginErrorText(page) {
  try {
    const text = await page.locator('body').innerText();
    const lines = String(text || '')
      .split(/\n+/)
      .map((line) => line.trim())
      .filter(Boolean);
    return (
      lines.find((line) =>
        /rate limit|重试|登录失败|密码错误|账号或密码|锁定|不准登录|验证码|too many/i.test(line),
      ) || ''
    );
  } catch (_) {
    return '';
  }
}

async function getRateLimitWaitMs(page) {
  try {
    const text = await page.locator('body').innerText();
    const seconds = text.match(/retry in (\d+) seconds/i);
    if (seconds) {
      return (Number.parseInt(seconds[1], 10) + 3) * 1000;
    }
    const minutes = text.match(/(\d+)\s*分钟后再(?:登录|试)/) || text.match(/retry in (\d+) minutes/i);
    if (minutes) {
      return (Number.parseInt(minutes[1], 10) * 60 + 3) * 1000;
    }
  } catch (_) {
    // ignore
  }
  return 0;
}

async function waitForStableNonLoginPage(page, stableMs = 1500, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  let nonLoginSince = 0;
  while (Date.now() < deadline) {
    if (!isLoginPageUrl(page.url())) {
      if (!nonLoginSince) {
        nonLoginSince = Date.now();
      }
      if (Date.now() - nonLoginSince >= stableMs) {
        return true;
      }
    } else {
      nonLoginSince = 0;
    }
    await page.waitForTimeout(200);
  }
  return false;
}

async function waitForLoginOutcome(page, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let nonLoginSince = 0;
  let nextErrorCheck = 0;
  while (Date.now() < deadline) {
    if (!isLoginPageUrl(page.url())) {
      if (!nonLoginSince) {
        nonLoginSince = Date.now();
      }
      if (Date.now() - nonLoginSince >= 1200) {
        return 'success';
      }
    } else {
      nonLoginSince = 0;
      if (Date.now() >= nextErrorCheck) {
        if (await hasLoginErrorBanner(page)) {
          return 'error';
        }
        nextErrorCheck = Date.now() + 500;
      }
    }
    await page.waitForTimeout(200);
  }
  return 'timeout';
}

async function waitForPageBodyText(page, minLength = 80, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const len = await page.evaluate(
        () => (document.body?.innerText || '').replace(/\s+/g, '').length,
      );
      if (len >= minLength) {
        return true;
      }
    } catch (_) {
      // ignore
    }
    await page.waitForTimeout(400);
  }
  return false;
}

/**
 * 按用例步骤号截图，自动使用 WHARTTEST_CASE_ID / SCREENSHOT_DIR，避免脚本里写引号路径。
 */
async function screenshotCaseStep(page, stepNumber, caseId) {
  const pathMod = require('path');
  const fs = require('fs');
  const dir = process.env.SCREENSHOT_DIR || '.';
  const cid = caseId || process.env.WHARTTEST_CASE_ID || 'unknown';
  const step = Number(stepNumber);
  const target = pathMod.join(dir, `case_${cid}_step${stepNumber}.png`);
  const url = page.url();
  const onLoginPage = isLoginPageUrl(url);
  const onDashboard = isDashboardUrl(url);
  const overviewCase = isOverviewCaseContext();
  const overviewSlaDetailCase = isOverviewSlaDetailCaseContext();
  const notificationCase = isNotificationCaseContext();

  // loginStep1 已在登录前提交步骤1图。登录后再截步骤1会覆盖成列表页。
  if (
    step === 1
    && !onLoginPage
    && fs.existsSync(target)
    && !overviewCase
    && !overviewSlaDetailCase
    && !notificationCase
  ) {
    console.log('[CASE_SCREENSHOT]', target);
    return target;
  }

  if (step >= 2 && onLoginPage) {
    console.log(
      `RESULT=FAIL: 步骤${step}截图时仍在登录页，loginStep1 未成功跳转，当前 URL=${url}`,
    );
    await dismissBlockingDialogs(page);
    await page.screenshot({
      path: target,
      fullPage: false,
      timeout: 8000,
      plain: true,
    });
    console.log('[CASE_SCREENSHOT]', target);
    return target;
  }

  if (step >= 2 && overviewSlaDetailCase) {
    if (step === 2 || onDashboard) {
      const ready = await ensureOverviewPageForScreenshot(page, step);
      if (!ready) {
        await page.screenshot({ path: target, fullPage: false, timeout: 8000 });
        console.log('[CASE_SCREENSHOT]', target);
        return target;
      }
      await waitForPageBodyText(page, 120, 15000);
      await captureDashboardStepView(page, target, Math.min(step, 7));
    } else if (isTicketDetailUrl(url)) {
      await captureTicketDetailView(page, target, step);
    } else {
      await captureMainContentView(page, target);
    }
    console.log('[CASE_SCREENSHOT]', target);
    return target;
  }

  if (step >= 2 && overviewCase) {
    const ready = await ensureOverviewPageForScreenshot(page, step);
    if (!ready) {
      await dismissBlockingDialogs(page);
      await page.screenshot({ path: target, fullPage: false, timeout: 8000 });
      console.log('[CASE_SCREENSHOT]', target);
      return target;
    }
    await waitForPageBodyText(page, 120, 15000);
    await captureDashboardStepView(page, target, step);
    console.log('[CASE_SCREENSHOT]', target);
    console.log(`RESULT=PASS: 步骤${step}截图完成 URL=${page.url()}`);
    return target;
  }

  if (step >= 2 && notificationCase) {
    const ready = await ensureNotificationPageForScreenshot(page, step);
    if (!ready) {
      console.log(`RESULT=FAIL: 步骤${step}未进入通知记录，拒绝上传「我的工单」截图`);
      return target;
    }
    await waitForPageBodyText(page, 120, 15000);
    const captured = await captureNotificationStepView(page, target, step);
    if (!captured) {
      console.log(`RESULT=FAIL: 步骤${step}通知记录截图失败 URL=${page.url()}`);
      return target;
    }
    console.log('[CASE_SCREENSHOT]', target);
    console.log(`RESULT=PASS: 步骤${step}截图完成 URL=${page.url()}`);
    return target;
  }

  const ticketDetailCase = isTicketDetailCaseContext();
  if (step >= 4 && ticketDetailCase && !(await isTicketDetailVisible(page))) {
    console.log(
      `RESULT=FAIL: 步骤${step}应在工单详情页截图，当前仍在列表或其他页 URL=${url}`,
    );
  }
  if (step >= 4 && ticketDetailCase && (await isTicketDetailVisible(page))) {
    await waitForPageBodyText(page, 80, 8000);
    await captureTicketDetailView(page, target, step);
    console.log('[CASE_SCREENSHOT]', target);
    return target;
  }

  const useMainContentCapture = step >= 2 && !onLoginPage;
  if (useMainContentCapture) {
    await waitForPageBodyText(page, 80, 4000);
    if (onDashboard) {
      await captureDashboardView(page, target);
    } else {
      const captured = await captureMainContentView(page, target);
      if (!captured) {
        await page.screenshot({
          path: target,
          fullPage: false,
          timeout: 8000,
          plain: true,
        });
      }
    }
  } else {
    await dismissBlockingDialogs(page);
    await page.screenshot({
      path: target,
      fullPage: false,
      timeout: 8000,
      plain: onLoginPage || step === 1,
    });
  }
  console.log('[CASE_SCREENSHOT]', target);
  return target;
}

function resolveLoginStep1Args(caseIdOrUsername, optionsOrPassword, maybeUrl) {
  if (optionsOrPassword && typeof optionsOrPassword === 'object' && !Array.isArray(optionsOrPassword)) {
    return { caseId: caseIdOrUsername, options: optionsOrPassword };
  }
  if (typeof caseIdOrUsername === 'string' && typeof optionsOrPassword === 'string') {
    return {
      caseId: process.env.WHARTTEST_CASE_ID,
      options: {
        username: caseIdOrUsername,
        password: optionsOrPassword,
        loginUrl: typeof maybeUrl === 'string' ? maybeUrl : undefined,
      },
    };
  }
  return { caseId: caseIdOrUsername, options: {} };
}

/**
 * 步骤1：填写账号后截登录页，再点击登录。步骤1图必须是登录页，禁止用登录后列表页覆盖。
 * 推荐：await helpers.loginStep1(page);
 * 也兼容误传 loginStep1(page, 用户名, 密码, 登录地址)。
 */
async function loginStep1(page, caseIdOrUsername, optionsOrPassword = {}, maybeUrl) {
  const { caseId, options } = resolveLoginStep1Args(
    caseIdOrUsername,
    optionsOrPassword,
    maybeUrl,
  );
  const pathMod = require('path');
  const dir = process.env.SCREENSHOT_DIR || '.';
  const cid = caseId || process.env.WHARTTEST_CASE_ID || 'unknown';
  const target = pathMod.join(dir, `case_${cid}_step1.png`);
  const username = options.username || process.env.WHARTTEST_USERNAME || '17670400361';
  const password = options.password || process.env.WHARTTEST_PASSWORD || '000000';
  const userPlaceholder = options.userPlaceholder || '请输入用户名';
  const passPlaceholder = options.passPlaceholder || '请输入密码';
  const submitName = options.submitName || '登 录';
  const rawLoginUrl = options.loginUrl || process.env.WHARTTEST_LOGIN_URL || '';
  const loginUrl = /^https?:\/\//i.test(String(rawLoginUrl))
    ? String(rawLoginUrl)
    : 'http://test.bot.by56.com/work-order/login';

  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(20000);

  if (!String(page.url() || '').includes('/login')) {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
  }
  await page.waitForTimeout(400);
  await dismissBlockingDialogs(page);

  await page.getByPlaceholder(userPlaceholder).fill(username, { timeout: 8000 });
  await page.getByPlaceholder(passPlaceholder).fill(password, { timeout: 8000 });
  await page.screenshot({ path: target, fullPage: false, timeout: 8000, plain: true });
  console.log('[CASE_SCREENSHOT]', target);

  let loginOk = false;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await page.getByRole('button', { name: submitName }).click({
      timeout: 8000,
      noWaitAfter: true,
    });
    const outcome = await waitForLoginOutcome(page, 10000);
    if (outcome === 'success') {
      loginOk = true;
      break;
    }
    loginOk = false;
    if (outcome === 'error') {
      const errText = await getLoginErrorText(page);
      const waitMs = await getRateLimitWaitMs(page);
      if (waitMs > 8000 || /锁定|分钟/.test(errText)) {
        await page.screenshot({ path: target, fullPage: false, timeout: 8000, plain: true }).catch(() => {});
        console.log(
          `RESULT=FAIL: 登录失败（账号限流/锁定）${errText ? '：' + errText : ''}，请等待解除后再执行`,
        );
        return target;
      }
      if (waitMs > 0 && attempt < 1) {
        const boundedWaitMs = Math.min(waitMs, 5000);
        console.log(
          `登录限流，等待 ${Math.round(boundedWaitMs / 1000)}s 后重试 (${attempt + 1}/2)`,
        );
        await page.waitForTimeout(boundedWaitMs);
        continue;
      }
      await page.screenshot({ path: target, fullPage: false, timeout: 8000, plain: true }).catch(() => {});
      console.log(
        `RESULT=FAIL: 登录失败${errText ? '：' + errText : '（页面报错或接口限流)'}，仍停留在登录页`,
      );
      return target;
    }
    if (attempt < 1) {
      await page.waitForTimeout(800);
    }
  }

  if (!loginOk || isLoginPageUrl(page.url())) {
    console.log(`RESULT=FAIL: 登录后未跳转，仍停留在登录页，URL=${page.url()}`);
    return target;
  }

  for (let i = 0; i < 3; i += 1) {
    const dismissed = await dismissBlockingDialogs(page);
    if (!dismissed) break;
    await page.waitForTimeout(200);
  }
  try {
    await page.getByRole('menuitem').first().waitFor({ state: 'visible', timeout: 4000 });
  } catch (_) {
    // 部分产品侧栏不是 menuitem，忽略
  }

  if (!await waitForStableNonLoginPage(page, 800, 3000)) {
    console.log(`RESULT=FAIL: 登录后又返回登录页，URL=${page.url()}`);
    return target;
  }

  console.log(`RESULT=PASS: 步骤1登录成功 URL=${page.url()}`);
  return target;
}

async function loginWorkOrderStep1(page, caseId) {
  return loginStep1(page, caseId);
}

/**
 * 截图并附加时间戳
 * @param {Object} page - Playwright 页面对象
 * @param {string} name - 截图名称
 * @param {Object} options - 截图配置
 */
async function takeScreenshot(page, name, options = {}) {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const filename = `${name}-${timestamp}.png`;

  await captureStepScreenshot(page, filename, options);
  
  console.log(`Screenshot saved: ${filename}`);
  return filename;
}

/**
 * 处理登录认证流程
 * @param {Object} page - Playwright 页面对象
 * @param {Object} credentials - 用户名与密码
 * @param {Object} selectors - 登录表单选择器
 */
async function authenticate(page, credentials, selectors = {}) {
  const defaultSelectors = {
    username: 'input[name="username"], input[name="email"], #username, #email',
    password: 'input[name="password"], #password',
    submit: 'button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Sign in")'
  };
  
  const finalSelectors = { ...defaultSelectors, ...selectors };
  
  await safeType(page, finalSelectors.username, credentials.username);
  await safeType(page, finalSelectors.password, credentials.password);
  await safeClick(page, finalSelectors.submit);
  
  // 等待页面跳转或登录成功标识
  await Promise.race([
    page.waitForNavigation({ waitUntil: 'networkidle' }),
    page.waitForSelector(selectors.successIndicator || '.dashboard, .user-menu, .logout', { timeout: 10000 })
  ]).catch(() => {
    console.log('Login might have completed without navigation');
  });
}

async function loginWorkOrderPortal(page, credentials = {}, loginUrl) {
  return loginStep1(page, undefined, {
    loginUrl,
    username: credentials.username,
    password: credentials.password,
  });
}

/**
 * 滚动页面
 * @param {Object} page - Playwright 页面对象
 * @param {string} direction - 滚动方向：'down'、'up'、'top'、'bottom'
 * @param {number} distance - 滚动像素（仅对 up/down 生效）
 */
async function scrollPage(page, direction = 'down', distance = 500) {
  switch (direction) {
    case 'down':
      await page.evaluate(d => window.scrollBy(0, d), distance);
      break;
    case 'up':
      await page.evaluate(d => window.scrollBy(0, -d), distance);
      break;
    case 'top':
      await page.evaluate(() => window.scrollTo(0, 0));
      break;
    case 'bottom':
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      break;
  }
  await page.waitForTimeout(500); // 等待滚动动画完成
}

/**
 * 提取表格数据
 * @param {Object} page - Playwright 页面对象
 * @param {string} tableSelector - 表格选择器
 */
async function extractTableData(page, tableSelector) {
  await page.waitForSelector(tableSelector);
  
  return await page.evaluate((selector) => {
    const table = document.querySelector(selector);
    if (!table) return null;
    
    const headers = Array.from(table.querySelectorAll('thead th')).map(th => 
      th.textContent?.trim()
    );
    
    const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr => {
      const cells = Array.from(tr.querySelectorAll('td'));
      if (headers.length > 0) {
        return cells.reduce((obj, cell, index) => {
          obj[headers[index] || `column_${index}`] = cell.textContent?.trim();
          return obj;
        }, {});
      } else {
        return cells.map(cell => cell.textContent?.trim());
      }
    });
    
    return { headers, rows };
  }, tableSelector);
}

async function ensureFilterPanelVisible(page, panelText = '筛选条件') {
  await dismissBlockingDialogs(page);
  const filterHeader = page.getByText(panelText, { exact: false }).first();
  if ((await filterHeader.count()) > 0) {
    await filterHeader.scrollIntoViewIfNeeded().catch(() => {});
  }
  await page.waitForTimeout(300);
}

async function selectFormDropdownOption(page, labelText, optionText) {
  await ensureFilterPanelVisible(page);
  let select = null;

  const filterField = page
    .locator('.filter-field')
    .filter({ has: page.locator('.filter-field-label', { hasText: labelText }) })
    .first();
  if ((await filterField.count()) > 0) {
    select = filterField.locator('.el-select').first();
  }

  if (!select || (await select.count()) === 0) {
    const formItem = page.locator('.el-form-item').filter({
      has: page.locator('label, .el-form-item__label, .filter-field-label').filter({
        hasText: new RegExp(`^\\s*${String(labelText).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`),
      }),
    }).first();
    if ((await formItem.count()) > 0) {
      select = formItem.locator('.el-select').first();
    }
  }

  if (!select || (await select.count()) === 0) {
    const labeled = page.locator('.filter-field, .el-form-item, .el-form-item__content').filter({
      has: page.getByText(labelText, { exact: true }),
    }).first();
    select = labeled.locator('.el-select').first();
  }

  await select.waitFor({ state: 'visible', timeout: 15000 });
  await select.click();
  const dropdown = page.locator('.el-select-dropdown.el-popper:visible, .el-select-dropdown:visible').last();
  await dropdown.waitFor({ state: 'visible', timeout: 10000 });
  const escaped = String(optionText).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const option = dropdown.locator('.el-select-dropdown__item').filter({
    hasText: new RegExp(`^\\s*${escaped}\\s*$`),
  });
  await option.first().click();
  await page.waitForTimeout(300);

  const shown = String((await select.innerText().catch(() => '')) || '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!shown.includes(String(optionText))) {
    console.log(
      `RESULT=FAIL: 下拉「${labelText}」未选中「${optionText}」，筛选框当前为「${shown || '空'}」。禁止把列表里的「${optionText}」当成已筛选。`,
    );
    return false;
  }
  console.log(`RESULT=PASS: 下拉「${labelText}」已选中「${optionText}」`);
  return true;
}

/**
 * 弹窗多选：点字段旁「+」/触发器，在弹窗里搜选项并确定。
 * fieldLabel / optionName 由当前用例步骤传入，不写死产品字段。
 */
async function selectDialogMultiSelect(page, fieldLabel, optionName, options = {}) {
  const dialogTitle = options.dialogTitle || `选择${fieldLabel}`;
  const confirmName = options.confirmName || /确定/;
  await ensureFilterPanelVisible(page);

  const escapedLabel = String(fieldLabel).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const labelRe = new RegExp(`^${escapedLabel}$`);

  const filterField = page
    .locator('.filter-field')
    .filter({ has: page.locator('.filter-field-label', { hasText: labelRe }) })
    .first();
  if ((await filterField.count()) > 0) {
    const poolTrigger = filterField.locator('.type-pool-trigger, button, [role="button"]').first();
    await poolTrigger.waitFor({ state: 'visible', timeout: 15000 });
    await poolTrigger.click();

    const dialog = page.locator('.el-dialog').filter({ hasText: dialogTitle }).last();
    await dialog.waitFor({ state: 'visible', timeout: 15000 });

    const search = dialog.locator('input[placeholder*="搜索"], input[placeholder*="名称"]').first();
    if ((await search.count()) > 0) {
      await search.fill(optionName);
      await page.waitForTimeout(600);
    }

    await dialog.getByText(optionName, { exact: true }).first().click();
    await dialog.getByRole('button', { name: confirmName }).click();
    await page.waitForTimeout(400);
    return;
  }

  let formItem = page
    .locator('.el-form-item')
    .filter({ has: page.locator('label, .el-form-item__label').filter({ hasText: new RegExp(`^${escapedLabel}`) }) })
    .first();
  if ((await formItem.count()) === 0) {
    formItem = page.locator('.el-form-item').filter({ hasText: new RegExp(escapedLabel) }).first();
  }
  await formItem.waitFor({ state: 'visible', timeout: 15000 });

  const select = formItem.locator('.el-select');
  if ((await select.count()) > 0) {
    await selectFormDropdownOption(page, fieldLabel, optionName);
    return;
  }

  const plusTrigger = formItem.locator(
    '.el-input-group__append, .el-input__suffix-inner, .el-input__suffix, button',
  ).filter({ hasText: '+' }).first();
  if ((await plusTrigger.count()) > 0) {
    await plusTrigger.click({ timeout: 5000 });
  } else {
    await formItem.locator('.el-input__wrapper, input').first().click({ timeout: 5000 });
  }

  const dialog = page.locator('.el-dialog:visible, .el-drawer:visible').last();
  await dialog.waitFor({ state: 'visible', timeout: 15000 });

  const search = dialog.locator(
    'input[placeholder*="搜索"], input[placeholder*="类型"], input[placeholder*="名称"]',
  ).first();
  if ((await search.count()) > 0) {
    await search.fill(optionName);
    await page.waitForTimeout(600);
  }

  const row = dialog.locator('.el-table__row, .el-checkbox-group label, li, .el-tree-node').filter({
    hasText: optionName,
  }).first();
  await row.waitFor({ state: 'visible', timeout: 10000 });
  const checkbox = row.locator('.el-checkbox').first();
  if ((await checkbox.count()) > 0) {
    await checkbox.click();
  } else {
    await row.click();
  }

  await dialog.getByRole('button', { name: confirmName }).click();
  await page.waitForTimeout(400);
}

async function selectTicketTypeInFilter(page, typeName) {
  return selectDialogMultiSelect(page, '工单类型', typeName, { dialogTitle: '选择工单类型' });
}

function toUrlPattern(urlPattern) {
  if (urlPattern === undefined || urlPattern === null || urlPattern === '') {
    return null;
  }
  if (urlPattern instanceof RegExp) {
    return urlPattern;
  }
  return new RegExp(String(urlPattern).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
}

function escapeRegExp(str) {
  return String(str).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

async function expandSubMenuIfNeeded(page, parentName) {
  const title = page.locator('.el-sub-menu__title').filter({
    hasText: new RegExp(`^\\s*${escapeRegExp(parentName)}`),
  }).first();
  if ((await title.count()) === 0) {
    return;
  }
  const subMenu = title.locator('xpath=ancestor::*[contains(@class,"el-sub-menu")][1]');
  const opened = await subMenu.evaluate((el) => el.classList.contains('is-opened')).catch(() => false);
  if (!opened) {
    await title.click({ timeout: 8000, force: true });
    await page.waitForTimeout(500);
  }
}

/**
 * 点击侧栏叶子菜单项。Element Plus 父级 submenu 的 accessible name 会包含全部子项名称，
 * 不能用 getByRole('menuitem', { name }) 子串匹配，否则会点到「工单中心」父节点而非「工单总览」。
 */
async function clickLeafMenuItem(page, menuName) {
  await dismissBlockingDialogs(page);
  const exactText = new RegExp(`^\\s*${escapeRegExp(menuName)}\\s*$`);
  const leaf = page.locator('.el-menu-item').filter({ hasText: exactText }).first();
  if ((await leaf.count()) > 0) {
    await leaf.waitFor({ state: 'attached', timeout: 8000 });
    try {
      await leaf.click({ timeout: 8000 });
    } catch (_) {
      await leaf.click({ timeout: 5000, force: true });
    }
    return true;
  }
  const menu = page.getByRole('menuitem', { name: menuName, exact: true });
  if ((await menu.count()) > 0) {
    await menu.first().click({ timeout: 8000, force: true });
    return true;
  }
  return false;
}

async function isLeafMenuActive(page, menuName) {
  const exactText = new RegExp(`^\\s*${escapeRegExp(menuName)}\\s*$`);
  const active = page.locator(
    '.el-menu-item.is-active, [role="menuitem"][aria-current="page"], '
      + '[role="menuitem"].active, nav a.active',
  ).filter({ hasText: exactText }).first();
  return (await active.count()) > 0
    && await active.isVisible().catch(() => false);
}

async function finishMenuNavigation(
  page,
  menuLabel,
  leafName,
  urlPattern,
  pattern,
  startUrl,
) {
  await page.waitForLoadState('networkidle').catch(() => {});
  await dismissBlockingDialogs(page);
  const currentUrl = page.url();
  const active = await isLeafMenuActive(page, leafName);
  const changed = currentUrl !== startUrl;
  if (isLoginPageUrl(currentUrl)) {
    console.log(
      `RESULT=FAIL: 导航菜单「${menuLabel}」时仍在登录页，当前=${currentUrl}`,
    );
  } else if (pattern && !pattern.test(currentUrl)) {
    console.log(`RESULT=FAIL: 导航失败，期望菜单「${menuLabel}」URL 含 ${urlPattern}，当前=${page.url()}`);
  } else if (!active && !changed) {
    console.log(
      `RESULT=FAIL: 导航菜单「${menuLabel}」后页面未变化，当前=${currentUrl}`,
    );
  } else {
    const contentOk = await waitForPageBodyText(page, 80, 12000);
    if (!contentOk) {
      console.log(
        `RESULT=FAIL: 菜单「${menuLabel}」页面内容过少，可能未加载完成，URL=${page.url()}`,
      );
    } else {
      console.log(`RESULT=PASS: 已进入菜单「${menuLabel}」 URL=${currentUrl}`);
    }
  }
  return page.url();
}

/**
 * 点侧栏菜单进入目标页，并用 URL 片段验收。菜单名/路径由用例步骤传入。
 */
async function navigateByMenu(page, menuName, urlPattern, fallbackPath) {
  const menuPath = String(menuName || '')
    .split(/\s*(?:->|=>|>|＞|→|\/)\s*/)
    .map((part) => part.trim().replace(/^[【\[]|[】\]]$/g, ''))
    .filter(Boolean);
  return navigateByMenuPath(
    page,
    menuPath.length > 0 ? menuPath : [menuName],
    urlPattern,
    fallbackPath,
  );
}

/**
 * 按「父菜单 > 子菜单」路径导航，先展开父级再点叶子项。
 */
async function navigateByMenuPath(page, menuPath, urlPattern, fallbackPath) {
  const path = Array.isArray(menuPath) ? menuPath : [menuPath];
  const menuLabel = path.join(' > ');
  const leafName = path[path.length - 1];
  const pattern = toUrlPattern(urlPattern);
  for (let i = 0; i < 3; i += 1) {
    await dismissBlockingDialogs(page);
  }
  const startUrl = page.url();
  if (isLoginPageUrl(startUrl)) {
    console.log(
      `RESULT=FAIL: 无法导航菜单「${menuLabel}」，当前仍在登录页 ${startUrl}`,
    );
    return startUrl;
  }
  if ((!pattern || pattern.test(startUrl)) && await isLeafMenuActive(page, leafName)) {
    return page.url();
  }

  let origin = '';
  const originCandidates = [
    page.url(),
    process.env.WHARTTEST_APP_ORIGIN,
    process.env.WHARTTEST_LOGIN_URL,
  ];
  for (const candidate of originCandidates) {
    if (!candidate) continue;
    try {
      const parsed = new URL(candidate);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        origin = parsed.origin;
        break;
      }
    } catch (_) {
      // try the next candidate
    }
  }

  try {
    for (let i = 0; i < path.length - 1; i += 1) {
      await expandSubMenuIfNeeded(page, path[i]);
    }
    const clicked = await clickLeafMenuItem(page, leafName);
    if (!clicked) {
      throw new Error(`未找到菜单项「${leafName}」`);
    }
    if (pattern && !pattern.test(startUrl)) {
      await page.waitForURL(pattern, { timeout: 8000 });
    } else {
      await page.waitForURL((url) => url.href !== startUrl, { timeout: 8000 });
    }
  } catch (_) {
    // 子菜单折叠或点击被遮挡时，直接走路由兜底
  }

  if (
    ((!pattern || !pattern.test(page.url())) || page.url() === startUrl)
    && fallbackPath
    && origin
  ) {
    const pathSuffix = fallbackPath.startsWith('/') ? fallbackPath : `/${fallbackPath}`;
    await page.goto(`${origin}${pathSuffix}`, { waitUntil: 'domcontentloaded' });
  }

  return finishMenuNavigation(
    page,
    menuLabel,
    leafName,
    urlPattern,
    pattern,
    startUrl,
  );
}

function detectWorkOrderPage(page) {
  const url = String(page.url() || '');
  if (/notifications/.test(url)) {
    return 'notification-records';
  }
  if (/\/work-order\/dashboard(?:\?|$|\/)/.test(url)) {
    return 'overview';
  }
  if (/\/work-order\/tickets\/\d+(?:\?|$|\/)/.test(url)) {
    return 'ticket-detail';
  }
  if (/\/work-order\/tickets(?:\?|$)/.test(url)) {
    return 'ticket-list';
  }
  if (/my-tickets/.test(url)) {
    return 'my-tickets';
  }
  return 'unknown';
}

async function navigateToOverviewPage(page) {
  await navigateByMenuPath(
    page,
    ['工单中心', '工单总览'],
    /\/work-order\/dashboard/,
    '/work-order/dashboard',
  );
  await assertOverviewPageLoaded(page);
  return page.url();
}

/**
 * 校验已进入数据总览页。主标题是 h2（不是 h1）；副标题以页面实际文案为准。
 */
async function assertOverviewPageLoaded(page) {
  if (!/\/work-order\/dashboard/.test(page.url())) {
    console.log(`RESULT=FAIL: 未进入数据总览，URL=${page.url()}`);
    return false;
  }
  await waitForPageBodyText(page, 120, 15000);

  const title = page.locator('h2').filter({ hasText: '数据总览' }).first();
  let titleOk = false;
  try {
    await title.waitFor({ state: 'visible', timeout: 10000 });
    titleOk = true;
  } catch (_) {
    titleOk = false;
  }

  const subtitleOk =
    (await page.locator('.page-desc, p, .page-header-content').filter({
      hasText: /快速(查看|盘点).*(工单|效率|风险|结构)/,
    }).count()) > 0;

  const timeOk =
    (await page.locator('.el-radio-button.is-active').filter({ hasText: '近7天' }).count()) > 0;

  if (!titleOk || !subtitleOk || !timeOk) {
    console.log(
      `RESULT=FAIL: 数据总览页校验失败 title=${titleOk} subtitle=${subtitleOk} 近7天=${timeOk} URL=${page.url()}`,
    );
    return false;
  }
  return true;
}

async function navigateToTicketListPage(page) {
  return navigateByMenuPath(
    page,
    ['工单中心', '工单列表'],
    /\/work-order\/tickets/,
    '/work-order/tickets',
  );
}

async function navigateToMyTicketsPage(page) {
  return navigateByMenu(page, '我的工单', /my-tickets/, '/work-order/my-tickets');
}

function isNotificationCaseContext() {
  return String(process.env.WHARTTEST_NOTIFICATION_CASE || '').trim() === '1';
}

function isNotificationRecordsUrl(url) {
  return /\/work-order\/notifications(?:\?|$|\/)/.test(String(url || ''));
}

async function navigateToNotificationRecordsPage(page) {
  await navigateByMenuPath(
    page,
    ['通知中心', '通知记录'],
    /\/work-order\/notifications/,
    '/work-order/notifications',
  );
  await assertNotificationRecordsPageLoaded(page);
  return page.url();
}

async function assertNotificationRecordsPageLoaded(page) {
  if (!isNotificationRecordsUrl(page.url())) {
    console.log(`RESULT=FAIL: 未进入通知记录页，URL=${page.url()}`);
    return false;
  }
  await waitForPageBodyText(page, 120, 15000);

  const titleOk =
    (await page.locator('h2').filter({ hasText: '通知记录' }).count()) > 0;
  const subtitleOk =
    (await page.locator('.page-desc, p, .page-header-content, main').filter({
      hasText: /查看所有.*(站内|内部).*(通知|企微|企业微信).*发送记录/,
    }).count()) > 0;

  if (!titleOk || !subtitleOk) {
    console.log(
      `RESULT=FAIL: 通知记录页校验失败 title=${titleOk} subtitle=${subtitleOk} URL=${page.url()}`,
    );
    return false;
  }
  return true;
}

async function assertNotificationStatsCards(page) {
  for (const label of ['待发送', '今日发送', '发送失败']) {
    try {
      await page.getByText(label, { exact: false }).first().waitFor({ state: 'visible', timeout: 10000 });
    } catch (_) {
      console.log(`RESULT=FAIL: 未找到统计卡片「${label}」`);
      return false;
    }
  }
  return true;
}

async function ensureNotificationPageForScreenshot(page, stepNumber) {
  if (Number(stepNumber) < 2) {
    return true;
  }
  if (isNotificationRecordsUrl(page.url())) {
    return true;
  }
  console.log(
    `步骤${stepNumber}截图前不在通知记录页，当前 URL=${page.url()}，尝试自动导航…`,
  );
  await navigateToNotificationRecordsPage(page);
  if (!isNotificationRecordsUrl(page.url())) {
    console.log(`RESULT=FAIL: 步骤${stepNumber}未能进入通知记录页，仍停在 ${page.url()}，禁止把「我的工单」当通知记录截图`);
    return false;
  }
  await waitForPageBodyText(page, 120, 15000);
  return true;
}

async function captureNotificationStepView(page, targetPath, stepNumber, options = {}) {
  const fs = require('fs');
  const step = Number(stepNumber);
  if (!isNotificationRecordsUrl(page.url())) {
    console.log(`RESULT=FAIL: 通知记录截图拒绝，当前页不是通知记录 URL=${page.url()}`);
    return false;
  }
  await dismissBlockingDialogs(page);
  await scrollMainContentToTop(page);

  const shotLocator = async (locator) => {
    if ((await locator.count()) === 0) {
      return false;
    }
    await locator.first().waitFor({ state: 'visible', timeout: 10000 });
    await locator.first().screenshot({ path: targetPath, ...options });
    return fs.existsSync(targetPath) && fs.statSync(targetPath).size >= 8000;
  };

  if (step === 2) {
    if (await shotLocator(page.locator('.page-header, .page-header-content'))) {
      return true;
    }
  }
  if (step === 3) {
    const stats = page.locator('.layout-content, .el-main').filter({ hasText: /待发送/ }).first();
    if (await shotLocator(stats)) {
      return true;
    }
  }
  if (step >= 4) {
    const table = page.locator('.el-table').first();
    if (await shotLocator(table)) {
      return true;
    }
  }
  return captureMainContentView(page, targetPath, options);
}

async function runNotificationCaseStep(page, stepNumber, caseId) {
  const step = Number(stepNumber);
  const cid = caseId || process.env.WHARTTEST_CASE_ID || 'unknown';
  const pathMod = require('path');
  const dir = process.env.SCREENSHOT_DIR || '.';
  const target = pathMod.join(dir, `case_${cid}_step${step}.png`);

  if (step === 1) {
    const path = await loginStep1(page, cid);
    if (!isLoginPageUrl(page.url())) {
      console.log(`RESULT=PASS: 步骤1登录成功 URL=${page.url()}`);
    }
    return path;
  }

  const ready = await ensureNotificationPageForScreenshot(page, step);
  if (!ready) {
    await screenshotCaseStep(page, step, cid);
    return null;
  }

  if (step === 2) {
    const ok = await assertNotificationRecordsPageLoaded(page);
    if (!ok) {
      await screenshotCaseStep(page, step, cid);
      return null;
    }
  } else if (step === 3) {
    const ok = await assertNotificationStatsCards(page);
    if (!ok) {
      await screenshotCaseStep(page, step, cid);
      return null;
    }
  } else if (step === 4) {
    for (const col of ['工单号', '事件类型', '接收人', '通知渠道', '通知状态']) {
      if ((await page.getByText(col, { exact: true }).count()) === 0) {
        console.log(`RESULT=FAIL: 列表表头缺少「${col}」`);
        await screenshotCaseStep(page, step, cid);
        return null;
      }
    }
  } else if (step === 5) {
    const row = page.locator('.el-table__body tr').first();
    try {
      await row.waitFor({ state: 'visible', timeout: 10000 });
    } catch (_) {
      console.log('RESULT=FAIL: 列表第一行未加载');
      await screenshotCaseStep(page, step, cid);
      return null;
    }
  }

  await captureNotificationStepView(page, target, step);
  console.log('[CASE_SCREENSHOT]', target);
  console.log(`RESULT=PASS: 步骤${step}完成 URL=${page.url()}`);
  return target;
}

function isTicketDetailCaseContext() {
  return String(process.env.WHARTTEST_TICKET_DETAIL_CASE || '').trim() === '1';
}

function getTicketNoFromEnv() {
  return String(process.env.WHARTTEST_TICKET_NO || '').trim();
}

function getTicketIdFromEnv() {
  return String(process.env.WHARTTEST_TICKET_ID || '').trim();
}

function isTicketDetailUrl(url) {
  return /\/work-order\/tickets\/\d+(?:\?|$|\/)/.test(String(url || ''));
}

function buildTicketDetailUrl(page, ticketId) {
  const id = String(ticketId || getTicketIdFromEnv()).trim();
  if (!id || !page) {
    return '';
  }
  const origin = new URL(page.url()).origin;
  return `${origin}/work-order/tickets/${id}`;
}

async function waitForTicketDetailReady(page, timeoutMs = 15000) {
  if (!page) {
    return false;
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (isTicketDetailUrl(page.url())) {
      try {
        const backVisible = await page
          .getByRole('button', { name: /返回列表/ })
          .first()
          .isVisible({ timeout: 800 });
        if (backVisible) {
          return true;
        }
      } catch (_) {
        // ignore
      }
      try {
        const detailVisible = await page
          .locator('main, .layout-content, .el-main')
          .filter({ hasText: /基本信息|暂无沟通记录|工单详情/ })
          .first()
          .isVisible({ timeout: 800 });
        if (detailVisible) {
          return true;
        }
      } catch (_) {
        // ignore
      }
    }
    await page.waitForTimeout(250);
  }
  return isTicketDetailUrl(page.url());
}

async function isTicketDetailVisible(page) {
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return false;
  }
  const strict = isTicketDetailCaseContext();
  if (!isTicketDetailUrl(page.url())) {
    return false;
  }
  if (strict) {
    return waitForTicketDetailReady(page, 8000);
  }
  try {
    if (
      await page
        .getByRole('button', { name: /返回列表/ })
        .first()
        .isVisible({ timeout: 2500 })
    ) {
      return true;
    }
  } catch (_) {
    // ignore
  }
  try {
    if (await page.getByText(/暂无沟通记录/).first().isVisible({ timeout: 2500 })) {
      return true;
    }
  } catch (_) {
    // ignore
  }
  return true;
}

async function waitForTicketDetailNavigation(page, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (isTicketDetailUrl(page.url()) && (await waitForTicketDetailReady(page, 1200))) {
      return true;
    }
    await page.waitForTimeout(400);
  }
  return isTicketDetailUrl(page.url()) && (await waitForTicketDetailReady(page, 2000));
}

async function gotoTicketDetailById(page, ticketId) {
  const detailUrl = buildTicketDetailUrl(page, ticketId);
  if (!detailUrl) {
    console.log('RESULT=FAIL: 缺少 ticketId，无法直达工单详情');
    return false;
  }
  if (isTicketDetailUrl(page.url()) && page.url().includes(`/tickets/${ticketId}`)) {
    return waitForTicketDetailReady(page, 5000);
  }
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  await dismissBlockingDialogs(page);
  try {
    await page.goto(detailUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
  } catch (err) {
    console.log(`RESULT=INFO: ticketId goto 首次异常 ${err?.message || err}，重试一次`);
    await page.waitForTimeout(800);
    try {
      await page.goto(detailUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    } catch (retryErr) {
      console.log(`RESULT=FAIL: ticketId goto 失败 ${retryErr?.message || retryErr}`);
      return false;
    }
  }
  await page.waitForLoadState('networkidle').catch(() => {});
  await dismissBlockingDialogs(page);
  if (!(await waitForTicketDetailNavigation(page, 20000))) {
    console.log(`RESULT=FAIL: ticketId 直达详情失败 URL=${page.url()}`);
    return false;
  }
  console.log(`RESULT=PASS: 已通过 ticketId 进入工单详情 URL=${page.url()}`);
  return true;
}

async function ensureTicketListPage(page) {
  const kind = detectWorkOrderPage(page);
  if (kind === 'ticket-list') {
    return true;
  }
  if (kind === 'ticket-detail') {
    try {
      await page.getByRole('button', { name: /返回列表/ }).click({ timeout: 8000 });
      await page.waitForURL(/\/work-order\/tickets(?:\?|$)/, { timeout: 12000 });
    } catch (_) {
      const origin = new URL(page.url()).origin;
      await page.goto(`${origin}/work-order/tickets`, { waitUntil: 'domcontentloaded' });
    }
    return detectWorkOrderPage(page) === 'ticket-list';
  }
  await navigateToTicketListPage(page);
  return detectWorkOrderPage(page) === 'ticket-list';
}

async function queryTicketInList(page, ticketNo) {
  const no = String(ticketNo || getTicketNoFromEnv()).trim();
  if (!no) {
    console.log('RESULT=FAIL: 缺少 ticketNo，无法查询工单');
    return false;
  }
  if (!(await ensureTicketListPage(page))) {
    console.log(`RESULT=FAIL: 未能进入工单列表 URL=${page.url()}`);
    return false;
  }
  await fillFilterField(page, '工单号', no);
  await clickPageButton(page, '查询');
  await page.waitForLoadState('networkidle').catch(() => {});
  const row = page.locator('.el-table__body tr').filter({ hasText: no }).first();
  try {
    await row.waitFor({ state: 'visible', timeout: 15000 });
  } catch (_) {
    console.log(`RESULT=FAIL: 查询后未找到工单 ${no}`);
    return false;
  }
  console.log(`RESULT=PASS: 已查询到目标工单 ${no}`);
  return true;
}

async function openTicketDetailFromList(page, ticketNo) {
  const no = String(ticketNo || getTicketNoFromEnv()).trim();
  const ticketId = getTicketIdFromEnv();
  if (!no) {
    console.log('RESULT=FAIL: 缺少 ticketNo，无法打开工单详情');
    return false;
  }
  if (await isTicketDetailVisible(page)) {
    console.log(`RESULT=PASS: 已在工单详情 URL=${page.url()}`);
    return true;
  }
  if (!isTicketDetailUrl(page.url()) && !(await ensureTicketListPage(page))) {
    console.log(`RESULT=FAIL: 打开详情前不在工单列表 URL=${page.url()}`);
    return false;
  }

  const row = page.locator('.el-table__body tr').filter({ hasText: no }).first();
  try {
    await row.waitFor({ state: 'visible', timeout: 15000 });
  } catch (_) {
    console.log(`RESULT=FAIL: 列表中未找到工单 ${no}`);
    return false;
  }

  let clicked = false;
  await dismissBlockingDialogs(page);
  const processBtn = row.getByRole('button', { name: '处理' }).first();
  await processBtn.scrollIntoViewIfNeeded().catch(() => {});
  try {
    await processBtn.click({ timeout: 10000 });
    clicked = true;
  } catch (_) {
    try {
      await dismissBlockingDialogs(page);
      await processBtn.evaluate((el) => el.click());
      clicked = true;
    } catch (_) {
      // fall through to ticketId goto
    }
  }

  if (await waitForTicketDetailNavigation(page, 18000)) {
    console.log(`RESULT=PASS: 已进入工单详情 URL=${page.url()}`);
    return true;
  }

  if (ticketId) {
    console.log(`RESULT=INFO: 点击「处理」未跳转，改用 ticketId=${ticketId} 直达详情`);
    if (await gotoTicketDetailById(page, ticketId)) {
      return true;
    }
  }

  console.log(
    `RESULT=FAIL: 点击「处理」后未进入工单详情 clicked=${clicked} URL=${page.url()}`,
  );
  return false;
}

async function assertCommunicationReadOnly(page) {
  await dismissBlockingDialogs(page);
  if (!(await isTicketDetailVisible(page))) {
    const ticketId = getTicketIdFromEnv();
    if (ticketId && (await gotoTicketDetailById(page, ticketId))) {
      await dismissBlockingDialogs(page);
    }
  }
  if (!(await isTicketDetailVisible(page))) {
    console.log(`RESULT=FAIL: 不在工单详情页，无法检查沟通区 URL=${page.url()}`);
    return false;
  }
  const sendButtons = await page.getByRole('button', { name: /发送|提交消息|回复消息/ }).count();
  const textareas = await page.locator('textarea:visible').count();
  if (sendButtons > 0 || textareas > 0) {
    console.log(
      `RESULT=FAIL: 页面存在消息发送控件 sendButtons=${sendButtons} textareas=${textareas}`,
    );
    return false;
  }
  console.log('RESULT=PASS: 沟通区只读，无发送控件');
  return true;
}

async function captureTicketDetailView(page, targetPath, stepNumber) {
  const fs = require('fs');
  const step = Number(stepNumber);
  if (!page || (typeof page.isClosed === 'function' && page.isClosed())) {
    return false;
  }
  await dismissBlockingDialogs(page);
  if (!(await isTicketDetailVisible(page))) {
    return captureMainContentView(page, targetPath);
  }

  const detailPanel = page
    .locator('.layout-content, .el-main, main')
    .filter({ hasText: /返回列表|基本信息|暂无沟通记录|工单摘要/ })
    .last();

  if (step >= 5) {
    await page.evaluate(() => {
      const marker = Array.from(document.querySelectorAll('*')).find((node) => {
        const text = (node.textContent || '').trim();
        return /暂无沟通记录|沟通记录|沟通区|消息记录/.test(text) && node.childElementCount < 16;
      });
      if (marker) {
        marker.scrollIntoView({ block: 'center', inline: 'nearest' });
      }
    });
    await page.waitForTimeout(400);
  } else {
    try {
      await page
        .getByRole('button', { name: /返回列表|返回/ })
        .first()
        .scrollIntoViewIfNeeded();
    } catch (_) {
      // ignore
    }
  }

  try {
    if ((await detailPanel.count()) > 0) {
      await detailPanel.first().screenshot({ path: targetPath, timeout: 8000 });
      if (fs.existsSync(targetPath)) {
        return true;
      }
    }
  } catch (_) {
    // fall through
  }

  try {
    const clip = await page.evaluate(() => {
      const anchor =
        Array.from(document.querySelectorAll('button, a, span')).find((node) =>
          /返回列表|返回/.test((node.textContent || '').trim()),
        ) || null;
      const host =
        (anchor && anchor.closest('.layout-content, .el-main, main')) ||
        document.querySelector('.layout-content, .el-main, main');
      if (!host) {
        return null;
      }
      const rect = host.getBoundingClientRect();
      const y = anchor ? Math.max(0, anchor.getBoundingClientRect().top - 12) : Math.max(0, rect.top);
      return {
        x: 0,
        y: Math.floor(y),
        width: window.innerWidth,
        height: Math.min(920, window.innerHeight - y - 8),
      };
    });
    if (clip && clip.width > 120 && clip.height > 120) {
      await page.screenshot({ path: targetPath, clip, fullPage: false, timeout: 8000 });
      if (fs.existsSync(targetPath)) {
        return true;
      }
    }
  } catch (_) {
    // fall through
  }

  return captureMainContentView(page, targetPath);
}

/**
 * 工单列表查询 + 进详情 + 沟通区只读类用例（如 case 1316）。
 * 每步只调用：await helpers.runTicketDetailCaseStep(page, <步骤号>);
 */
async function runTicketDetailCaseStep(page, stepNumber, caseId) {
  const step = Number(stepNumber);
  const cid = caseId || process.env.WHARTTEST_CASE_ID || 'unknown';
  const pathMod = require('path');
  const dir = process.env.SCREENSHOT_DIR || '.';
  const target = pathMod.join(dir, `case_${cid}_step${step}.png`);
  const ticketNo = getTicketNoFromEnv();

  if (step === 1) {
    return loginStep1(page, cid);
  }

  if (step === 2) {
    await navigateToTicketListPage(page);
    await waitForPageBodyText(page, 80, 10000);
    await screenshotCaseStep(page, 2, cid);
    console.log(`RESULT=PASS: 步骤2已进入工单列表 URL=${page.url()}`);
    return target;
  }

  if (step === 3) {
    const ok = await queryTicketInList(page, ticketNo);
    await screenshotCaseStep(page, 3, cid);
    if (ok) {
      console.log(`RESULT=PASS: 步骤3已查询到目标工单 URL=${page.url()}`);
    }
    return ok ? target : null;
  }

  if (step === 4) {
    const ok = await openTicketDetailFromList(page, ticketNo);
    await captureTicketDetailView(page, target, 4);
    console.log('[CASE_SCREENSHOT]', target);
    if (ok) {
      console.log(`RESULT=PASS: 步骤4已进入工单详情 URL=${page.url()}`);
    }
    return ok ? target : null;
  }

  if (step >= 5) {
    if (!(await isTicketDetailVisible(page))) {
      const ticketId = getTicketIdFromEnv();
      if (!(ticketId && (await gotoTicketDetailById(page, ticketId)))) {
        await openTicketDetailFromList(page, ticketNo);
      }
    }
    const ok = await assertCommunicationReadOnly(page);
    await captureTicketDetailView(page, target, step);
    console.log('[CASE_SCREENSHOT]', target);
    if (ok) {
      console.log(`RESULT=PASS: 步骤${step}沟通区检查通过 URL=${page.url()}`);
    }
    return ok ? target : null;
  }

  return screenshotCaseStep(page, step, cid);
}

/**
 * 按字段筛选并点查询。字段标签由调用方传入。
 */
async function filterByFields(page, filters = {}) {
  const searchName = filters.searchButton || '查询';
  for (const item of filters.dialogSelects || []) {
    await selectDialogMultiSelect(page, item.fieldLabel, item.option, {
      dialogTitle: item.dialogTitle,
    });
  }
  for (const item of filters.dropdowns || []) {
    await selectFormDropdownOption(page, item.fieldLabel, item.option);
  }
  await page.getByRole('button', { name: searchName }).click();
  await page.waitForLoadState('networkidle').catch(() => {});
  await page
    .locator('.el-table__body tr, table tbody tr')
    .first()
    .waitFor({ state: 'visible', timeout: 15000 })
    .catch(() => {});
}

async function filterTicketList(page, filters = {}) {
  const { ticketType, status, approvalStatus } = filters;
  const dialogSelects = ticketType
    ? [{ fieldLabel: '工单类型', option: ticketType, dialogTitle: '选择工单类型' }]
    : [];
  const dropdowns = [];
  if (status) {
    dropdowns.push({ fieldLabel: '工单状态', option: status });
  }
  if (approvalStatus) {
    const approvalLabel = (await page.getByText('审批情况', { exact: true }).count()) > 0
      ? '审批情况'
      : '审批状态';
    dropdowns.push({ fieldLabel: approvalLabel, option: approvalStatus });
  }
  return filterByFields(page, { dialogSelects, dropdowns });
}

async function filterWorkOrdersByStatus(page, statusText) {
  return filterByFields(page, {
    dropdowns: [{ fieldLabel: '工单状态', option: statusText }],
  });
}

/**
 * 只点「我知道了」这类确认按钮，不会点「确定/取消/关闭」，以免关掉正在测的业务弹窗。
 * @param {Object} page - Playwright 页面对象
 * @returns {Promise<boolean>} 是否关掉了至少一个弹窗
 */
async function dismissBlockingDialogs(page) {
  if (!page) return false;
  try {
    if (typeof page.isClosed === 'function' && page.isClosed()) return false;
  } catch (e) {
    return false;
  }

  const clickIfVisible = async (locator, label) => {
    try {
      const target = locator.first();
      if (!(await target.isVisible())) return false;
      await target.click({ timeout: 2000, force: true });
      console.log(`已关闭遮挡弹窗: ${label}`);
      return true;
    } catch (e) {
      return false;
    }
  };

  let dismissed = false;

  for (let round = 0; round < 4; round += 1) {
    let dismissedThisRound = false;

    try {
      const versionDialog = page
        .locator('.el-dialog, [role="dialog"], .el-overlay-dialog')
        .filter({ hasText: '发现新版本' });
      if (await versionDialog.first().isVisible().catch(() => false)) {
        dismissedThisRound =
          (await clickIfVisible(
            versionDialog.getByRole('button', { name: /我知道了|知道了/ }),
            '发现新版本',
          )) || dismissedThisRound;
      }
    } catch (e) {
      // ignore
    }

    dismissedThisRound =
      (await clickIfVisible(
        page.getByRole('button', { name: '我知道了', exact: true }),
        '我知道了',
      )) || dismissedThisRound;

    try {
      const overlayAck = page
        .locator('.el-overlay:visible, .el-overlay-dialog:visible')
        .getByRole('button', { name: /我知道了|知道了|暂不|以后再说/ });
      dismissedThisRound =
        (await clickIfVisible(overlayAck, '通用弹窗确认')) || dismissedThisRound;
    } catch (e) {
      // ignore
    }

    if (!isTicketDetailUrl(page.url())) {
      dismissedThisRound =
        (await clickIfVisible(
          page.locator('.el-drawer__close-btn, .el-drawer .el-drawer__close, .el-drawer [aria-label="关闭"]'),
          '侧栏抽屉',
        )) || dismissedThisRound;
    }

    if (!dismissedThisRound) {
      break;
    }
    dismissed = true;
    await page.waitForTimeout(350);
  }

  const hasDialog = await page.locator('.el-dialog:visible, [role="dialog"]:visible').count();
  const hasDrawer = await page.locator('.el-drawer:visible, .el-popper:visible').count();
  const onTicketDetail = isTicketDetailUrl(page.url());
  if (
    !onTicketDetail &&
    (hasDrawer > 0 || (hasDialog === 0 && (await page.locator('.el-overlay:visible').count()) > 0))
  ) {
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(200);
    dismissed = true;
  }
  if (hasDialog === 0) {
    const mask = page.locator('.el-overlay:visible').first();
    if (await mask.isVisible().catch(() => false)) {
      await mask.click({ position: { x: 10, y: 10 }, timeout: 1500, force: true }).catch(() => {});
      console.log('已关闭遮挡弹窗: 遮罩层');
      dismissed = true;
    }
  }

  return dismissed;
}

async function clickPageButton(page, name) {
  await dismissBlockingDialogs(page);
  const raw = String(name || '').trim();
  const stripped = raw.replace(/^[+\uFF0B]\s*/, '');
  const names = stripped && stripped !== raw ? [raw, stripped] : [raw];
  let lastErr;
  for (const n of names) {
    const btn = page.getByRole('button', { name: n }).first();
    try {
      await btn.click({ timeout: 8000 });
      return;
    } catch (err) {
      lastErr = err;
      await dismissBlockingDialogs(page);
      await page.keyboard.press('Escape').catch(() => {});
      await page.waitForTimeout(200);
      try {
        await btn.click({ timeout: 5000, force: true });
        return;
      } catch (err2) {
        lastErr = err2;
      }
    }
  }
  throw lastErr;
}

/**
 * 断言页面上出现可见文案。必填星号常是独立节点，不要用 getByText('字段*')。
 * 会忽略文案末尾的 *，并跳过隐藏节点。
 */
async function assertPageShows(page, labels, timeoutMs = 4000) {
  const list = (Array.isArray(labels) ? labels : [labels])
    .map((item) => String(item || '').trim())
    .filter(Boolean);
  const missing = [];
  for (const raw of list) {
    const text = raw.replace(/[＊*]\s*$/g, '').trim();
    const loc = page.getByText(text, { exact: false });
    const deadline = Date.now() + timeoutMs;
    let visible = false;
    while (Date.now() < deadline) {
      const count = await loc.count();
      for (let i = 0; i < count; i++) {
        if (await loc.nth(i).isVisible().catch(() => false)) {
          visible = true;
          break;
        }
      }
      if (visible) break;
      await page.waitForTimeout(200);
    }
    if (!visible) missing.push(raw);
  }
  if (missing.length) {
    console.log(`RESULT=FAIL: 未找到可见文案: ${missing.join(', ')}`);
    return false;
  }
  console.log(`RESULT=PASS: 已找到 ${list.join(', ')}`);
  return true;
}

/**
 * 在列表中按行内文本点操作按钮，并用「返回 / 标题」判断是否进入详情，不写死 URL。
 */
async function clickRowAction(page, rowText, buttonName) {
  await dismissBlockingDialogs(page);
  const row = page.getByRole('row').filter({ hasText: String(rowText) }).first();
  await row.waitFor({ state: 'visible', timeout: 12000 });
  const btn = row.getByRole('button', { name: buttonName }).first();
  await btn.scrollIntoViewIfNeeded().catch(() => {});
  try {
    await btn.click({ timeout: 8000 });
  } catch (_) {
    await dismissBlockingDialogs(page);
    await btn.click({ timeout: 5000, force: true });
  }
  await page.waitForTimeout(400);
  await dismissBlockingDialogs(page);

  const entered = await Promise.race([
    page.waitForURL(/\/work-order\/tickets\/\d+/, { timeout: 12000 }).then(() => true),
    page
      .getByRole('button', { name: /返回列表|返回/ })
      .first()
      .waitFor({ state: 'visible', timeout: 12000 })
      .then(() => true),
    page
      .locator('main, .layout-content, .el-main')
      .filter({ hasText: /基本信息|暂无沟通记录/ })
      .first()
      .waitFor({ state: 'visible', timeout: 12000 })
      .then(() => true),
    page.waitForTimeout(12000).then(() => false),
  ]);
  if (!entered) {
    console.log(`RESULT=FAIL: 已点「${buttonName}」但未见详情（当前 URL=${page.url()}）`);
  }
  return page.url();
}

/**
 * 等待并关闭 Cookie 提示条
 * @param {Object} page - Playwright 页面对象
 * @param {number} timeout - 最大等待时间
 */
async function handleCookieBanner(page, timeout = 3000) {
  const commonSelectors = [
    'button:has-text("Accept")',
    'button:has-text("Accept all")',
    'button:has-text("OK")',
    'button:has-text("Got it")',
    'button:has-text("I agree")',
    '.cookie-accept',
    '#cookie-accept',
    '[data-testid="cookie-accept"]'
  ];
  
  for (const selector of commonSelectors) {
    try {
      const element = await page.waitForSelector(selector, { 
        timeout: timeout / commonSelectors.length,
        state: 'visible'
      });
      if (element) {
        await element.click();
        console.log('Cookie banner dismissed');
        return true;
      }
    } catch (e) {
      // 当前选择器失败，继续尝试下一个
    }
  }
  
  return false;
}

/**
 * 使用指数退避重试函数
 * @param {Function} fn - 待重试函数
 * @param {number} maxRetries - 最大重试次数
 * @param {number} initialDelay - 初始延迟（毫秒）
 */
async function retryWithBackoff(fn, maxRetries = 3, initialDelay = 1000) {
  let lastError;
  
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      const delay = initialDelay * Math.pow(2, i);
      console.log(`Attempt ${i + 1} failed, retrying in ${delay}ms...`);
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }
  
  throw lastError;
}

/**
 * 使用通用配置创建浏览器上下文
 * @param {Object} browser - 浏览器实例
 * @param {Object} options - 上下文配置
 */
async function createContext(browser, options = {}) {
  const envHeaders = getExtraHeadersFromEnv();

  // 将环境变量请求头与传入配置合并
  const mergedHeaders = {
    ...envHeaders,
    ...options.extraHTTPHeaders
  };

  const defaultOptions = {
    viewport: { width: 1280, height: 900 },
    userAgent: options.mobile
      ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_7_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Mobile/15E148 Safari/604.1'
      : undefined,
    permissions: options.permissions || [],
    geolocation: options.geolocation,
    locale: options.locale || 'en-US',
    timezoneId: options.timezoneId || 'America/New_York',
    // 仅在存在请求头时附加 extraHTTPHeaders
    ...(Object.keys(mergedHeaders).length > 0 && { extraHTTPHeaders: mergedHeaders })
  };

  return await browser.newContext({ ...defaultOptions, ...options });
}

/**
 * 在常见端口检测正在运行的开发服务器
 * @param {Array<number>} customPorts - 额外要检测的端口
 * @returns {Promise<Array>} 检测到的服务 URL 列表
 */
async function detectDevServers(customPorts = []) {
  const http = require('http');

  // 常见开发服务器端口
  const commonPorts = [3000, 3001, 3002, 5173, 8080, 8000, 4200, 5000, 9000, 1234];
  const allPorts = [...new Set([...commonPorts, ...customPorts])];

  const detectedServers = [];

  console.log('🔍 Checking for running dev servers...');

  for (const port of allPorts) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.request({
          hostname: 'localhost',
          port: port,
          path: '/',
          method: 'HEAD',
          timeout: 500
        }, (res) => {
          if (res.statusCode < 500) {
            detectedServers.push(`http://localhost:${port}`);
            console.log(`  ✅ Found server on port ${port}`);
          }
          resolve();
        });

        req.on('error', () => resolve());
        req.on('timeout', () => {
          req.destroy();
          resolve();
        });

        req.end();
      });
    } catch (e) {
      // 端口不可用，继续检测
    }
  }

  if (detectedServers.length === 0) {
    console.log('  ❌ No dev servers detected');
  }

  return detectedServers;
}

/**
 * 获取可读页面文本（便于 AI 理解）
 * @param {Object} page - Playwright 页面对象
 * @returns {string} 页面文本内容
 */
async function getPageText(page) {
  return await page.innerText('body');
}

/**
 * 获取页面结构，帮助 AI 理解可交互元素
 * 返回表单、按钮、链接、输入框等简化结构
 * @param {Object} page - Playwright 页面对象
 * @returns {Object} 页面结构信息（表单/按钮/链接/输入框）
 */
async function getPageStructure(page) {
  return await page.evaluate(() => {
    const getSelector = (el) => {
      if (el.id) return `#${el.id}`;
      if (el.name) return `[name="${el.name}"]`;
      if (el.type) return `${el.tagName.toLowerCase()}[type="${el.type}"]`;
      if (el.className) return `${el.tagName.toLowerCase()}.${el.className.split(' ')[0]}`;
      return el.tagName.toLowerCase();
    };

    const inputs = Array.from(document.querySelectorAll('input, textarea, select')).map(el => ({
      type: el.type || el.tagName.toLowerCase(),
      name: el.name || null,
      id: el.id || null,
      placeholder: el.placeholder || null,
      selector: getSelector(el),
      value: el.type === 'password' ? '***' : (el.value || null)
    }));

    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]')).map(el => ({
      text: el.innerText?.trim() || el.value || null,
      type: el.type || null,
      selector: getSelector(el)
    }));

    const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 20).map(el => ({
      text: el.innerText?.trim() || null,
      href: el.href,
      selector: getSelector(el)
    }));

    const headings = Array.from(document.querySelectorAll('h1, h2, h3')).map(el => ({
      level: el.tagName,
      text: el.innerText?.trim()
    }));

    return {
      title: document.title,
      url: window.location.href,
      headings,
      inputs,
      buttons,
      links: links.filter(l => l.text)
    };
  });
}

/**
 * 以可读格式生成页面描述（供 AI 使用）
 * @param {Object} page - Playwright 页面对象
 * @returns {string} 人类与 AI 都易读的页面描述
 */
async function describePageForAI(page) {
  const structure = await getPageStructure(page);
  let desc = `## Page: ${structure.title}\nURL: ${structure.url}\n\n`;

  if (structure.headings.length > 0) {
    desc += `### Headings\n`;
    structure.headings.forEach(h => { desc += `- ${h.level}: ${h.text}\n`; });
    desc += '\n';
  }

  if (structure.inputs.length > 0) {
    desc += `### Input Fields (${structure.inputs.length})\n`;
    structure.inputs.forEach(i => {
      desc += `- ${i.type}: selector="${i.selector}"`;
      if (i.placeholder) desc += ` placeholder="${i.placeholder}"`;
      if (i.name) desc += ` name="${i.name}"`;
      desc += '\n';
    });
    desc += '\n';
  }

  if (structure.buttons.length > 0) {
    desc += `### Buttons (${structure.buttons.length})\n`;
    structure.buttons.forEach(b => {
      desc += `- "${b.text || 'no text'}": selector="${b.selector}"\n`;
    });
    desc += '\n';
  }

  if (structure.links.length > 0) {
    desc += `### Links (showing ${structure.links.length})\n`;
    structure.links.forEach(l => {
      desc += `- "${l.text}": ${l.href}\n`;
    });
  }

  return desc;
}

async function fillFilterField(page, labelText, value) {
  await dismissBlockingDialogs(page);
  await ensureFilterPanelVisible(page);
  const filterField = page
    .locator('.filter-field')
    .filter({ has: page.locator('.filter-field-label', { hasText: labelText }) })
    .first();
  if ((await filterField.count()) > 0) {
    const input = filterField.locator('input').first();
    await input.waitFor({ state: 'visible', timeout: 8000 });
    await input.fill(String(value));
    return;
  }
  await page.getByPlaceholder(new RegExp(labelText)).fill(String(value), { timeout: 8000 });
}

module.exports = {
  launchBrowser,
  createPage,
  waitForPageReady,
  safeClick,
  safeType,
  extractTexts,
  takeScreenshot,
  scrollMainContentToTable,
  scrollMainContentToTop,
  prepareStepScreenshot,
  captureStepScreenshot,
  captureMainContentView,
  captureDashboardView,
  captureTicketListView,
  authenticate,
  loginWorkOrderPortal,
  screenshotCaseStep,
  loginStep1,
  loginWorkOrderStep1,
  scrollPage,
  extractTableData,
  dismissBlockingDialogs,
  fillFilterField,
  clickPageButton,
  assertPageShows,
  clickRowAction,
  selectFormDropdownOption,
  selectDialogMultiSelect,
  selectTicketTypeInFilter,
  filterByFields,
  filterWorkOrdersByStatus,
  detectWorkOrderPage,
  navigateByMenu,
  navigateByMenuPath,
  navigateToOverviewPage,
  assertOverviewPageLoaded,
  assertOverviewKpiCards,
  runOverviewCaseStep,
  runOverviewSlaDetailCaseStep,
  navigateToNotificationRecordsPage,
  assertNotificationRecordsPageLoaded,
  runNotificationCaseStep,
  captureNotificationStepView,
  navigateToTicketListPage,
  navigateToMyTicketsPage,
  isTicketDetailVisible,
  queryTicketInList,
  openTicketDetailFromList,
  assertCommunicationReadOnly,
  captureTicketDetailView,
  runTicketDetailCaseStep,
  filterTicketList,
  handleCookieBanner,
  retryWithBackoff,
  createContext,
  detectDevServers,
  getExtraHeadersFromEnv,
  getPageText,
  getPageStructure,
  describePageForAI
};
