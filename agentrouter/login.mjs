import { appendFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const LOGIN_URL = 'https://agentrouter.org/login';
const AUTH_CHECK_URL = 'https://agentrouter.org/console/topup';
const LOGIN_TIMEOUT_MS = 30_000;
const BEIJING_TIME_FORMATTER = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

export function parseAccounts(rawAccounts) {
  if (typeof rawAccounts !== 'string' || rawAccounts.trim() === '') {
    throw new Error('未配置 GitHub Secret：ACCOUNTS_JSON');
  }

  let parsed;
  try {
    parsed = JSON.parse(rawAccounts);
  } catch {
    throw new Error('ACCOUNTS_JSON 不是有效的 JSON');
  }

  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('ACCOUNTS_JSON 必须是非空数组');
  }

  return parsed.map((account, index) => {
    const position = index + 1;
    if (!account || typeof account !== 'object' || Array.isArray(account)) {
      throw new Error(`第 ${position} 个账号必须是对象`);
    }

    for (const field of ['name', 'username', 'password']) {
      if (typeof account[field] !== 'string' || account[field].trim() === '') {
        throw new Error(`第 ${position} 个账号缺少有效的 ${field}`);
      }
    }

    let socks5 = null;
    if (account.socks5 !== undefined && account.socks5 !== null) {
      if (typeof account.socks5 !== 'string') {
        throw new Error(`第 ${position} 个账号的 socks5 不是有效 URL`);
      }

      socks5 = account.socks5.trim() || null;
    }

    if (socks5) {
      let proxyUrl;
      try {
        proxyUrl = new URL(socks5);
      } catch {
        throw new Error(`第 ${position} 个账号的 socks5 不是有效 URL`);
      }

      const proxyPort = Number(proxyUrl.port);
      if (
        proxyUrl.protocol !== 'socks5:' ||
        proxyUrl.hostname === '' ||
        !Number.isInteger(proxyPort) ||
        proxyPort < 1 ||
        proxyPort > 65535
      ) {
        throw new Error(`第 ${position} 个账号的 socks5 必须是无认证 socks5://host:port`);
      }

      if (proxyUrl.username || proxyUrl.password) {
        throw new Error(`第 ${position} 个账号的 socks5 不支持用户名密码认证`);
      }
    }

    return {
      name: account.name.trim().replace(/\s+/g, ' '),
      username: account.username,
      password: account.password,
      socks5,
    };
  });
}

function escapeWorkflowCommand(value) {
  return value
    .replaceAll('%', '%25')
    .replaceAll('\r', '%0D')
    .replaceAll('\n', '%0A');
}

function maskCredentials(accounts) {
  if (process.env.GITHUB_ACTIONS !== 'true') {
    return;
  }

  for (const account of accounts) {
    console.log(`::add-mask::${escapeWorkflowCommand(account.username)}`);
    console.log(`::add-mask::${escapeWorkflowCommand(account.password)}`);
    if (account.socks5) {
      console.log(`::add-mask::${escapeWorkflowCommand(account.socks5)}`);
    }
  }
}

function getSensitiveValues(account) {
  const values = [account.username, account.password, account.socks5];

  if (account.socks5) {
    try {
      const proxyUrl = new URL(account.socks5);
      values.push(proxyUrl.host, proxyUrl.hostname, proxyUrl.username, proxyUrl.password);
      for (const value of [proxyUrl.username, proxyUrl.password]) {
        if (value) {
          try {
            values.push(decodeURIComponent(value));
          } catch {
            // Keep the encoded value when it is not valid percent-encoding.
          }
        }
      }
    } catch {
      // parseAccounts has already validated configured proxy URLs.
    }
  }

  return [...new Set(values.filter(Boolean))].sort((left, right) => right.length - left.length);
}

function sanitizeText(value, account, maxLength = 300) {
  let message = String(value ?? '未知');

  for (const secret of getSensitiveValues(account)) {
    message = message.replaceAll(secret, '***');
  }

  return message.replace(/\s+/g, ' ').trim().slice(0, maxLength);
}

function sanitizeError(error, account) {
  return sanitizeText(error instanceof Error ? error.message : error, account);
}

export function formatPageDiagnostics({ network, status, url, title }, account) {
  let safeUrl = '未知';
  try {
    const parsedUrl = new URL(url);
    safeUrl = `${parsedUrl.origin}${parsedUrl.pathname}`;
  } catch {
    // Keep an unknown URL out of the diagnostic rather than logging raw input.
  }

  return [
    `网络=${sanitizeText(network, account, 40)}`,
    `HTTP=${status ?? '未知'}`,
    `URL=${sanitizeText(safeUrl, account, 180)}`,
    `标题=${sanitizeText(title, account, 120)}`,
  ].join('；');
}

async function getPageDiagnostics(page, session, account) {
  let url = '未知';
  let title = '未知';

  try {
    url = page.url();
  } catch {
    // The page may already be closed after a navigation failure.
  }

  try {
    title = await page.title();
  } catch {
    // A title is optional diagnostic context.
  }

  return formatPageDiagnostics(
    {
      network: session.network,
      status: session.lastStatus,
      url,
      title,
    },
    account,
  );
}

async function runLoginStep(stage, session, account, action) {
  try {
    return await action();
  } catch (error) {
    const diagnostics = await getPageDiagnostics(session.page, session, account);
    throw new Error(`${stage}失败（${diagnostics}）：${sanitizeError(error, account)}`);
  }
}

export function formatBeijingTime(date = new Date()) {
  const parts = Object.fromEntries(
    BEIJING_TIME_FORMATTER
      .formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  );

  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

export async function runAccounts(accounts, loginAccount, log = console.log) {
  const results = [];

  for (const [index, account] of accounts.entries()) {
    log(`[${index + 1}/${accounts.length}] ${account.name}：开始登录`);

    try {
      await loginAccount(account);
      results.push({
        name: account.name,
        username: account.username,
        success: true,
        time: formatBeijingTime(),
      });
      log(`[${index + 1}/${accounts.length}] ${account.name}：登录成功`);
    } catch (error) {
      const message = sanitizeError(error, account);
      results.push({
        name: account.name,
        username: account.username,
        success: false,
        time: formatBeijingTime(),
        message,
      });
      log(`[${index + 1}/${accounts.length}] ${account.name}：登录失败 - ${message}`);
    }
  }

  return results;
}

async function createLoginSession(browser, socks5, network) {
  let context;

  try {
    context = await browser.newContext({
      locale: 'zh-CN',
      ...(socks5 ? { proxy: { server: socks5 } } : {}),
    });
    const page = await context.newPage();
    page.setDefaultTimeout(LOGIN_TIMEOUT_MS);
    page.setDefaultNavigationTimeout(LOGIN_TIMEOUT_MS);

    const response = await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded' });
    if (response && !response.ok()) {
      throw new Error(`登录页返回 HTTP ${response.status()}`);
    }

    return {
      context,
      page,
      network,
      lastStatus: typeof response?.status === 'function' ? response.status() : null,
    };
  } catch (error) {
    if (context) {
      await context.close();
    }
    throw error;
  }
}

export async function openLoginSession(browser, account, log = console.log) {
  if (!account.socks5) {
    try {
      return await createLoginSession(browser, null, 'GitHub Actions 直连');
    } catch (error) {
      throw new Error(`GitHub Actions 直连打开登录页失败 - ${sanitizeError(error, account)}`);
    }
  }

  try {
    return await createLoginSession(browser, account.socks5, 'SOCKS5');
  } catch (error) {
    log(`${account.name}：SOCKS5 打开登录页失败 - ${sanitizeError(error, account)}`);
    log(`${account.name}：改用 GitHub Actions 网络直连`);

    try {
      return await createLoginSession(browser, null, 'GitHub Actions 直连');
    } catch (directError) {
      throw new Error(`GitHub Actions 直连打开登录页失败 - ${sanitizeError(directError, account)}`);
    }
  }
}

export async function verifyAuthenticatedPage(page, session = null) {
  const response = await page.goto(AUTH_CHECK_URL, { waitUntil: 'domcontentloaded' });
  if (session && typeof response?.status === 'function') {
    session.lastStatus = response.status();
  }
  const finalUrl = new URL(page.url());

  if (
    !response ||
    !response.ok() ||
    finalUrl.origin !== 'https://agentrouter.org' ||
    !finalUrl.pathname.startsWith('/console')
  ) {
    throw new Error('登录状态验证失败：无法访问受保护的控制台页面');
  }
}

export async function waitForLoginEntry(page) {
  const usernameInput = page.locator('#username');
  const emailLoginButton = page.getByRole('button', { name: /使用 邮箱或用户名 登录/ });
  const visibleEntry = await Promise.any([
    usernameInput.waitFor({ state: 'visible' }).then(() => 'username'),
    emailLoginButton.waitFor({ state: 'visible' }).then(() => 'email'),
  ]);
  return { usernameInput, emailLoginButton, visibleEntry };
}

async function loginWithBrowser(browser, account) {
  const session = await openLoginSession(browser, account);
  const { context, page } = session;

  try {
    const { usernameInput, emailLoginButton, visibleEntry } = await runLoginStep(
      '等待登录入口',
      session,
      account,
      () => waitForLoginEntry(page),
    );

    if (visibleEntry === 'email') {
      await runLoginStep(
        '打开邮箱登录表单',
        session,
        account,
        () => emailLoginButton.click(),
      );
    }

    await runLoginStep(
      '等待账号输入框',
      session,
      account,
      () => usernameInput.waitFor({ state: 'visible' }),
    );
    await runLoginStep(
      '填写账号',
      session,
      account,
      () => usernameInput.fill(account.username),
    );

    const passwordInput = page.locator('#password');
    await runLoginStep(
      '等待密码输入框',
      session,
      account,
      () => passwordInput.waitFor({ state: 'visible' }),
    );
    await runLoginStep(
      '填写密码',
      session,
      account,
      () => passwordInput.fill(account.password),
    );

    const continueButton = page.getByRole('button', { name: '继续', exact: true });
    await runLoginStep(
      '等待继续按钮',
      session,
      account,
      () => continueButton.waitFor({ state: 'visible' }),
    );

    await runLoginStep(
      '提交登录',
      session,
      account,
      () => Promise.all([
        page.waitForURL(
          (url) => url.origin === 'https://agentrouter.org' && url.pathname !== '/login',
          { waitUntil: 'domcontentloaded' },
        ),
        continueButton.click(),
      ]),
    );

    await runLoginStep(
      '验证登录状态',
      session,
      account,
      () => verifyAuthenticatedPage(page, session),
    );
  } finally {
    await context.close();
  }
}

function escapeMarkdown(value) {
  return value.replaceAll('|', '\\|').replaceAll('\r', ' ').replaceAll('\n', ' ');
}

export function formatAccountResults(results) {
  const rows = results.map((result) => {
    const status = result.success ? '成功' : '失败';
    return `| ${escapeMarkdown(result.username)} | ${status} | ${result.time} |`;
  });

  return [
    '| 用户名 | 结果 | 时间 |',
    '| --- | --- | --- |',
    ...rows,
    '',
  ].join('\n');
}

async function writeAccountResults(results) {
  if (!process.env.LOGIN_RESULTS_FILE) {
    return;
  }

  await writeFile(process.env.LOGIN_RESULTS_FILE, formatAccountResults(results), 'utf8');
}

async function writeSummary(results) {
  const succeeded = results.filter((result) => result.success).length;
  const failed = results.length - succeeded;

  console.log(`\n执行完成：成功 ${succeeded} 个，失败 ${failed} 个。`);

  if (!process.env.GITHUB_STEP_SUMMARY) {
    return;
  }

  const rows = results.map((result) => {
    const status = result.success ? '成功' : '失败';
    const note = result.success ? '-' : escapeMarkdown(result.message);
    return `| ${escapeMarkdown(result.name)} | ${status} | ${note} |`;
  });

  const summary = [
    '# Agent Router 登录结果',
    '',
    `成功：${succeeded}，失败：${failed}`,
    '',
    '| 账号别名 | 结果 | 说明 |',
    '| --- | --- | --- |',
    ...rows,
    '',
  ].join('\n');

  await appendFile(process.env.GITHUB_STEP_SUMMARY, summary, 'utf8');
}

async function main() {
  const accounts = parseAccounts(process.env.ACCOUNTS_JSON);
  maskCredentials(accounts);

  const { chromium } = await import('playwright');
  const browser = await chromium.launch({ headless: true });

  let results;
  try {
    results = await runAccounts(accounts, (account) => loginWithBrowser(browser, account));
  } finally {
    await browser.close();
  }

  await writeAccountResults(results);
  await writeSummary(results);

  if (results.some((result) => !result.success)) {
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`执行失败：${message.split('\n')[0]}`);
    process.exitCode = 1;
  });
}
