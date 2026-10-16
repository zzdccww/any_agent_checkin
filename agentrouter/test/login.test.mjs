import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatAccountResults,
  formatBeijingTime,
  formatPageDiagnostics,
  openLoginSession,
  parseAccounts,
  runAccounts,
  verifyAuthenticatedPage,
  waitForLoginEntry,
} from '../login.mjs';

test('parseAccounts parses account fields with SOCKS5', () => {
  const accounts = parseAccounts(JSON.stringify([
    {
      name: ' 账号 1 ',
      username: 'user@example.com',
      password: 'secret',
      socks5: 'socks5://208.102.51.6:58208',
    },
  ]));

  assert.deepEqual(accounts, [
    {
      name: '账号 1',
      username: 'user@example.com',
      password: 'secret',
      socks5: 'socks5://208.102.51.6:58208',
    },
  ]);
});

test('parseAccounts accepts missing or empty SOCKS5', () => {
  const accounts = parseAccounts(JSON.stringify([
    {
      name: '账号 1',
      username: 'one@example.com',
      password: 'secret-1',
    },
    {
      name: '账号 2',
      username: 'two@example.com',
      password: 'secret-2',
      socks5: '  ',
    },
  ]));

  assert.equal(accounts[0].socks5, null);
  assert.equal(accounts[1].socks5, null);
});

test('parseAccounts rejects missing credentials', () => {
  assert.throws(
    () => parseAccounts(JSON.stringify([
      {
        name: '账号 1',
        username: '',
        password: 'secret',
        socks5: 'socks5://208.102.51.6:58208',
      },
    ])),
    /username/,
  );
});

test('parseAccounts rejects authenticated or non-SOCKS5 proxies', () => {
  const base = { name: '账号 1', username: 'user', password: 'secret' };

  assert.throws(
    () => parseAccounts(JSON.stringify([{ ...base, socks5: 'socks5://user:pass@host:1080' }])),
    /不支持用户名密码认证/,
  );
  assert.throws(
    () => parseAccounts(JSON.stringify([{ ...base, socks5: 'http://host:8080' }])),
    /无认证 socks5/,
  );
});

function createFakeBrowser({ failProxy = false } = {}) {
  const options = [];
  const contexts = [];
  const browser = {
    async newContext(contextOptions) {
      options.push(contextOptions);
      const context = {
        closed: false,
        async newPage() {
          return {
            setDefaultTimeout() {},
            setDefaultNavigationTimeout() {},
            async goto() {
              if (failProxy && contextOptions.proxy) {
                throw new Error(`proxy unavailable at ${contextOptions.proxy.server}`);
              }

              return { ok: () => true, status: () => 200 };
            },
          };
        },
        async close() {
          context.closed = true;
        },
      };
      contexts.push(context);
      return context;
    },
  };

  return { browser, options, contexts };
}

test('openLoginSession uses direct network when SOCKS5 is missing', async () => {
  const fake = createFakeBrowser();
  const session = await openLoginSession(
    fake.browser,
    { name: '账号 1', username: 'one', password: 'secret', socks5: null },
    () => {},
  );

  assert.deepEqual(fake.options, [{ locale: 'zh-CN' }]);
  assert.equal(session.network, 'GitHub Actions 直连');
  assert.equal(session.lastStatus, 200);
  await session.context.close();
});

test('openLoginSession keeps using SOCKS5 when it can connect', async () => {
  const fake = createFakeBrowser();
  const logs = [];
  const session = await openLoginSession(
    fake.browser,
    {
      name: '账号 1',
      username: 'one',
      password: 'secret',
      socks5: 'socks5://proxy.example:1080',
    },
    (message) => logs.push(message),
  );

  assert.deepEqual(fake.options, [
    { locale: 'zh-CN', proxy: { server: 'socks5://proxy.example:1080' } },
  ]);
  assert.deepEqual(logs, []);
  assert.equal(session.network, 'SOCKS5');
  assert.equal(session.lastStatus, 200);
  await session.context.close();
});

test('openLoginSession falls back to direct network when SOCKS5 cannot connect', async () => {
  const fake = createFakeBrowser({ failProxy: true });
  const logs = [];
  const session = await openLoginSession(
    fake.browser,
    {
      name: '账号 1',
      username: 'one',
      password: 'secret',
      socks5: 'socks5://proxy.example:1080',
    },
    (message) => logs.push(message),
  );

  assert.deepEqual(fake.options, [
    { locale: 'zh-CN', proxy: { server: 'socks5://proxy.example:1080' } },
    { locale: 'zh-CN' },
  ]);
  assert.equal(fake.contexts[0].closed, true);
  assert.deepEqual(logs, [
    '账号 1：SOCKS5 打开登录页失败 - proxy unavailable at ***',
    '账号 1：改用 GitHub Actions 网络直连',
  ]);
  assert.doesNotMatch(logs.join('\n'), /proxy\.example|1080/);
  assert.equal(session.network, 'GitHub Actions 直连');
  await session.context.close();
});

test('formatPageDiagnostics removes secrets and URL query data', () => {
  const diagnostics = formatPageDiagnostics(
    {
      network: 'SOCKS5',
      status: 200,
      url: 'https://agentrouter.org/login?token=secret#fragment',
      title: 'Welcome user@example.com through proxy.example',
    },
    {
      username: 'user@example.com',
      password: 'secret',
      socks5: 'socks5://proxy.example:1080',
    },
  );

  assert.equal(
    diagnostics,
    '网络=SOCKS5；HTTP=200；URL=https://agentrouter.org/login；标题=Welcome *** through ***',
  );
  assert.doesNotMatch(diagnostics, /user@example\.com|secret|proxy\.example|1080|token=/);
});

test('waitForLoginEntry waits until a login control is visible', async () => {
  const calls = [];
  const emailLoginButton = {
    kind: 'email-login-button',
    async waitFor(options) {
      calls.push(['email.waitFor', options]);
      return new Promise(() => {});
    },
  };
  const usernameInput = {
    async waitFor(options) {
      calls.push(['username.waitFor', options]);
    },
  };
  const page = {
    locator(selector) {
      assert.equal(selector, '#username');
      return usernameInput;
    },
    getByRole(role, options) {
      assert.equal(role, 'button');
      assert.match('使用 邮箱或用户名 登录', options.name);
      return emailLoginButton;
    },
  };

  const result = await waitForLoginEntry(page);

  assert.deepEqual(result, { usernameInput, emailLoginButton, visibleEntry: 'username' });
  assert.deepEqual(calls, [
    ['username.waitFor', { state: 'visible' }],
    ['email.waitFor', { state: 'visible' }],
  ]);
});

test('verifyAuthenticatedPage accepts an authenticated console page', async () => {
  const visits = [];
  const page = {
    async goto(url, options) {
      visits.push({ url, options });
      return { ok: () => true };
    },
    url() {
      return 'https://agentrouter.org/console/topup';
    },
  };

  await verifyAuthenticatedPage(page);
  assert.deepEqual(visits, [
    {
      url: 'https://agentrouter.org/console/topup',
      options: { waitUntil: 'domcontentloaded' },
    },
  ]);
});

test('verifyAuthenticatedPage rejects a redirect back to login', async () => {
  const page = {
    async goto() {
      return { ok: () => true };
    },
    url() {
      return 'https://agentrouter.org/login';
    },
  };

  await assert.rejects(
    () => verifyAuthenticatedPage(page),
    /登录状态验证失败/,
  );
});

test('verifyAuthenticatedPage rejects a failed protected-page response', async () => {
  const page = {
    async goto() {
      return { ok: () => false };
    },
    url() {
      return 'https://agentrouter.org/console/topup';
    },
  };

  await assert.rejects(
    () => verifyAuthenticatedPage(page),
    /登录状态验证失败/,
  );
});

test('formatBeijingTime converts UTC time to Beijing time', () => {
  assert.equal(
    formatBeijingTime(new Date('2026-08-08T00:07:09Z')),
    '2026-08-08 08:07:09',
  );
});

test('formatAccountResults records every username, status, and time', () => {
  const markdown = formatAccountResults([
    {
      name: '账号 1',
      username: 'one|mail@example.com',
      success: true,
      time: '2026-08-08 08:07:10',
    },
    {
      name: '账号 2',
      username: 'two@example.com',
      success: false,
      time: '2026-08-08 08:07:20',
      message: 'private failure detail',
    },
  ]);

  assert.equal(markdown, [
    '| 用户名 | 结果 | 时间 |',
    '| --- | --- | --- |',
    '| one\\|mail@example.com | 成功 | 2026-08-08 08:07:10 |',
    '| two@example.com | 失败 | 2026-08-08 08:07:20 |',
    '',
  ].join('\n'));
  assert.doesNotMatch(markdown, /private failure detail/);
});

test('runAccounts stays sequential and continues after a failure', async () => {
  const accounts = [
    { name: '账号 1', username: 'one', password: 'one-secret', socks5: 'socks5://host1:1001' },
    { name: '账号 2', username: 'two', password: 'two-secret', socks5: 'socks5://host2:1002' },
    { name: '账号 3', username: 'three', password: 'three-secret', socks5: 'socks5://host3:1003' },
  ];
  const order = [];
  let active = 0;
  let maxActive = 0;

  const results = await runAccounts(
    accounts,
    async (account) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      order.push(account.name);

      try {
        await new Promise((resolve) => setTimeout(resolve, 5));
        if (account.name === '账号 2') {
          throw new Error(`login failed for ${account.username}`);
        }
      } finally {
        active -= 1;
      }
    },
    () => {},
  );

  assert.equal(maxActive, 1);
  assert.deepEqual(order, ['账号 1', '账号 2', '账号 3']);
  assert.deepEqual(results.map(({ time, ...result }) => result), [
    { name: '账号 1', username: 'one', success: true },
    { name: '账号 2', username: 'two', success: false, message: 'login failed for ***' },
    { name: '账号 3', username: 'three', success: true },
  ]);
  for (const result of results) {
    assert.match(result.time, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  }
});
