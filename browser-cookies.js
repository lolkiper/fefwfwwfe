'use strict';

/**
 * Читает вход YouTube из Edge или Chrome на Windows.
 *
 * yt-dlp копирует файл Cookies обычным копированием и падает с
 * «Could not copy Chrome cookie database», пока браузер (или его фоновый
 * процесс) держит базу. Новые Edge и Chrome ещё и шифруют cookies так,
 * что этот файл бесполезен без самого браузера. Поэтому браузер на секунду
 * перезапускается с портом отладки, cookies забираются уже расшифрованными
 * и сохраняются в формате Netscape для `--cookies`.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const net = require('net');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');

const DEBUG_PORT = 9337;
const WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const AUTH_COOKIE = /^(SID|SSID|HSID|LOGIN_INFO|SAPISID|__Secure-1PSID|__Secure-3PSID|__Secure-1PAPISID|__Secure-3PAPISID)$/;

const BROWSERS = {
  edge: {
    label: 'Edge',
    image: 'msedge.exe',
    reject: null,
    exe(env) {
      return firstExisting([
        path.join(env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        path.join(env.PROGRAMFILES || 'C:\\Program Files', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        path.join(env.LOCALAPPDATA || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe')
      ]);
    },
    userData(env) {
      return path.join(env.LOCALAPPDATA || '', 'Microsoft', 'Edge', 'User Data');
    }
  },
  chrome: {
    label: 'Chrome',
    image: 'chrome.exe',
    reject: /Edg\//i,
    exe(env) {
      return firstExisting([
        path.join(env.PROGRAMFILES || 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
        path.join(env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
        path.join(env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe')
      ]);
    },
    userData(env) {
      return path.join(env.LOCALAPPDATA || '', 'Google', 'Chrome', 'User Data');
    }
  }
};

function firstExisting(candidates) {
  return candidates.find((file) => file && fs.existsSync(file)) || null;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isYoutubeFamily(domain) {
  const host = String(domain || '').replace(/^\./, '').toLowerCase();
  return /(^|\.)((youtube|youtube-nocookie|googlevideo|ytimg|google|googleusercontent)\.com)$/.test(host);
}

function hasYouTubeSession(cookies) {
  return (cookies || []).some((cookie) =>
    cookie && cookie.value && AUTH_COOKIE.test(String(cookie.name || '')) && isYoutubeFamily(cookie.domain));
}

function writeNetscapeCookies(file, cookies) {
  const lines = ['# Netscape HTTP Cookie File', ''];
  (cookies || []).forEach((cookie) => {
    if (!cookie || !cookie.name || cookie.value == null) return;
    if (!isYoutubeFamily(cookie.domain)) return;
    const domain = String(cookie.domain);
    const includeSub = domain.startsWith('.') ? 'TRUE' : 'FALSE';
    const secure = cookie.secure ? 'TRUE' : 'FALSE';
    const expires = !cookie.expires || cookie.expires < 0 || cookie.session ? '0' : String(Math.floor(Number(cookie.expires)));
    const value = String(cookie.value).replace(/[\t\r\n]/g, '');
    const name = String(cookie.name).replace(/[\t\r\n]/g, '');
    if (!name || !value) return;
    lines.push([domain, includeSub, cookie.path || '/', secure, expires, name, value].join('\t'));
  });
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${lines.join('\n')}\n`, 'utf8');
  return file;
}

function listBrowserProfiles(userData) {
  if (!userData || !fs.existsSync(userData)) return ['Default'];
  let lastUsed = null;
  try {
    const state = JSON.parse(fs.readFileSync(path.join(userData, 'Local State'), 'utf8'));
    lastUsed = state && state.profile && state.profile.last_used;
  } catch {
    lastUsed = null;
  }
  let names = [];
  try {
    names = fs.readdirSync(userData, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && /^(Default|Profile \d+)$/.test(entry.name))
      .map((entry) => entry.name);
  } catch {
    names = [];
  }
  if (!names.length) names = ['Default'];
  const mtime = (name) => {
    const candidates = [
      path.join(userData, name, 'Network', 'Cookies'),
      path.join(userData, name, 'Cookies')
    ];
    for (const file of candidates) {
      try {
        return fs.statSync(file).mtimeMs;
      } catch {
        /* следующий путь */
      }
    }
    return 0;
  };
  names.sort((a, b) => {
    if (a === lastUsed) return -1;
    if (b === lastUsed) return 1;
    return mtime(b) - mtime(a);
  });
  return names;
}

function runCommand(command, args) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, { windowsHide: true, stdio: 'ignore' });
    } catch {
      resolve(1);
      return;
    }
    child.on('error', () => resolve(1));
    child.on('exit', (code) => resolve(code == null ? 1 : code));
  });
}

function browserIsRunning(image) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn('tasklist', ['/FI', `IMAGENAME eq ${image}`, '/NH'], { windowsHide: true });
    } catch {
      resolve(false);
      return;
    }
    let out = '';
    child.stdout.on('data', (chunk) => {
      out += chunk.toString();
    });
    child.on('error', () => resolve(false));
    child.on('exit', () => resolve(out.toLowerCase().includes(String(image).toLowerCase())));
  });
}

async function stopBrowser(image) {
  await runCommand('taskkill', ['/F', '/IM', image, '/T']);
  for (let i = 0; i < 20; i += 1) {
    if (!(await browserIsRunning(image))) return;
    await sleep(250);
  }
}

function startBrowser(exe, args) {
  const child = spawn(exe, args, { detached: true, stdio: 'ignore', windowsHide: false });
  child.unref();
}

function requestJson(url, method) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const req = http.request({
      hostname: parsed.hostname,
      port: parsed.port,
      path: `${parsed.pathname}${parsed.search}`,
      method,
      family: 4,
      timeout: 2000
    }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch (err) {
          reject(err);
        }
      });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    req.end();
  });
}

async function debugPageSocket(port, browserSocket) {
  const target = `http://127.0.0.1:${port}/json/new?about:blank`;
  for (const method of ['PUT', 'GET']) {
    try {
      const page = await requestJson(target, method);
      if (page && page.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {
      /* старый Edge открывает вкладку другим методом */
    }
  }
  return browserSocket;
}

async function waitForDebugger(port, timeoutMs, shouldStop) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (shouldStop && shouldStop()) return null;
    try {
      const info = await requestJson(`http://127.0.0.1:${port}/json/version`, 'GET');
      if (info && info.webSocketDebuggerUrl) return info;
    } catch {
      /* порт ещё не слушает */
    }
    await sleep(400);
  }
  return null;
}

function tryReadFrame(buffer) {
  if (buffer.length < 2) return null;
  const first = buffer[0];
  const second = buffer[1];
  const opcode = first & 0x0f;
  const fin = (first & 0x80) !== 0;
  const masked = (second & 0x80) !== 0;
  let length = second & 0x7f;
  let offset = 2;
  if (length === 126) {
    if (buffer.length < 4) return null;
    length = buffer.readUInt16BE(2);
    offset = 4;
  } else if (length === 127) {
    if (buffer.length < 10) return null;
    const big = buffer.readBigUInt64BE(2);
    if (big > BigInt(8 * 1024 * 1024)) throw new Error('слишком большой ответ браузера');
    length = Number(big);
    offset = 10;
  }
  const maskLen = masked ? 4 : 0;
  if (buffer.length < offset + maskLen + length) return null;
  let payload = buffer.slice(offset + maskLen, offset + maskLen + length);
  if (masked) {
    const mask = buffer.slice(offset, offset + 4);
    payload = Buffer.from(payload);
    for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4];
  }
  return {
    opcode,
    fin,
    payload,
    rest: buffer.slice(offset + maskLen + length)
  };
}

function encodeClientFrame(text) {
  const payload = Buffer.from(String(text));
  const mask = crypto.randomBytes(4);
  let header;
  if (payload.length < 126) {
    header = Buffer.from([0x81, 0x80 | payload.length]);
  } else if (payload.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = 0x80 | 127;
    header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  const masked = Buffer.alloc(payload.length);
  for (let i = 0; i < payload.length; i += 1) masked[i] = payload[i] ^ mask[i % 4];
  return Buffer.concat([header, mask, masked]);
}

function connectWebSocket(wsUrl, timeoutMs) {
  return new Promise((resolve, reject) => {
    const url = new URL(wsUrl);
    const socket = net.connect({ host: url.hostname, port: Number(url.port), family: 4 });
    const key = crypto.randomBytes(16).toString('base64');
    let buf = Buffer.alloc(0);
    let opened = false;
    let failed = false;
    const fail = (err) => {
      if (failed || opened) return;
      failed = true;
      socket.destroy();
      reject(err instanceof Error ? err : new Error(String(err || 'браузер не ответил')));
    };
    const timer = setTimeout(() => fail(new Error('браузер не ответил')), timeoutMs);
    socket.on('error', (err) => fail(err));
    socket.on('data', (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (opened) return;
      const end = buf.indexOf('\r\n\r\n');
      if (end < 0) return;
      const status = buf.slice(0, end).toString('utf8').split('\r\n')[0] || '';
      if (!/\s101\s/.test(status)) {
        clearTimeout(timer);
        fail(new Error('не удалось подключиться к браузеру'));
        return;
      }
      buf = buf.slice(end + 4);
      opened = true;
      clearTimeout(timer);
      resolve({
        socket,
        pull() {
          const current = buf;
          buf = Buffer.alloc(0);
          return current;
        }
      });
    });
    socket.write(
      `GET ${url.pathname}${url.search} HTTP/1.1\r\n` +
        `Host: ${url.host}\r\n` +
        'Upgrade: websocket\r\n' +
        'Connection: Upgrade\r\n' +
        `Sec-WebSocket-Key: ${key}\r\n` +
        'Sec-WebSocket-Version: 13\r\n\r\n'
    );
  });
}

function waitForSocketData(socket, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      socket.off('data', onData);
      resolve(false);
    }, timeoutMs);
    const onData = () => {
      clearTimeout(timer);
      resolve(true);
    };
    socket.once('data', onData);
  });
}

async function readYoutubeCookiesFromDebugger(wsUrl, timeoutMs = 8000) {
  const client = await connectWebSocket(wsUrl, timeoutMs);
  const { socket } = client;
  try {
    socket.write(encodeClientFrame(JSON.stringify({
      id: 1,
      method: 'Network.getCookies',
      params: {
        urls: [
          'https://www.youtube.com/',
          'https://youtube.com/',
          'https://www.google.com/',
          'https://accounts.google.com/'
        ]
      }
    })));
    const started = Date.now();
    let pending = client.pull();
    let text = '';
    let opcode = 0;
    while (Date.now() - started < timeoutMs) {
      const frame = tryReadFrame(pending);
      if (!frame) {
        const arrived = await waitForSocketData(socket, Math.max(1, timeoutMs - (Date.now() - started)));
        pending = Buffer.concat([pending, client.pull()]);
        if (!arrived && !pending.length) break;
        continue;
      }
      pending = frame.rest;
      if (frame.opcode === 8) break;
      if (frame.opcode === 1 || frame.opcode === 0) {
        if (frame.opcode === 1) {
          text = '';
          opcode = 1;
        }
        text += frame.payload.toString('utf8');
        if (frame.fin && opcode === 1) {
          const message = JSON.parse(text);
          if (message.error) throw new Error(message.error.message || 'браузер не отдал cookies');
          const cookies = message.result && message.result.cookies;
          return Array.isArray(cookies) ? cookies : [];
        }
      }
    }
    throw new Error('браузер не отдал cookies');
  } finally {
    socket.destroy();
  }
}

async function readProfileCookies(browser, profile, options) {
  const port = options.port || DEBUG_PORT;
  const shouldStop = options.shouldStop;
  const wasRunning = await browserIsRunning(browser.image);
  if (wasRunning) await stopBrowser(browser.image);
  if (shouldStop && shouldStop()) return [];
  startBrowser(browser.exe(options.env || process.env), [
    `--remote-debugging-port=${port}`,
    `--profile-directory=${profile}`,
    '--restore-last-session',
    '--no-first-run',
    '--no-default-browser-check'
  ]);
  const info = await waitForDebugger(port, 20000, shouldStop);
  if (!info || !info.webSocketDebuggerUrl) return [];
  if (browser.reject && browser.reject.test(String(info.Browser || ''))) return [];
  const wsUrl = await debugPageSocket(port, info.webSocketDebuggerUrl);
  let cookies = [];
  for (let attempt = 0; attempt < 5; attempt += 1) {
    if (shouldStop && shouldStop()) return [];
    try {
      cookies = await readYoutubeCookiesFromDebugger(wsUrl, 8000);
    } catch {
      cookies = [];
    }
    if (cookies.length) break;
    await sleep(500);
  }
  return cookies;
}

async function exportBrowserCookies(name, options) {
  const browser = BROWSERS[name];
  if (!browser) return null;
  const env = options.env || process.env;
  const exe = browser.exe(env);
  const userData = browser.userData(env);
  if (!exe) return null;
  const log = typeof options.log === 'function' ? options.log : () => {};
  const running = await browserIsRunning(browser.image);
  log(running
    ? `${browser.label} ещё запущен и держит файл cookies. Закрываю окно на секунду и читаю вход YouTube.`
    : `${browser.label} закрыт — открываю его, чтобы прочитать вход YouTube.`);
  const profiles = listBrowserProfiles(userData).slice(0, 3);
  let found = null;
  try {
    for (const profile of profiles) {
      if (options.shouldStop && options.shouldStop()) return null;
      const cookies = await readProfileCookies(browser, profile, options);
      if (hasYouTubeSession(cookies)) {
        found = cookies;
        break;
      }
    }
  } finally {
    await stopBrowser(browser.image);
    if (running && exe) {
      startBrowser(exe, ['--restore-last-session']);
    }
  }
  if (!found) {
    log(`В ${browser.label} нет входа в YouTube. Откройте youtube.com, войдите в аккаунт и запустите скачивание ещё раз.`);
    return null;
  }
  const dest = path.join(options.destDir || os.tmpdir(), 'shorts-inserter', 'youtube-cookies.txt');
  writeNetscapeCookies(dest, found);
  log(`Вход YouTube прочитан из ${browser.label}.`);
  return dest;
}

/**
 * Пишет cookies.txt и возвращает путь. На не-Windows возвращает null:
 * там остаётся прежний --cookies-from-browser.
 */
async function exportYoutubeCookies(options = {}) {
  const platform = options.platform || process.platform;
  if (platform !== 'win32') return null;
  const order = options.browsers || ['edge', 'chrome'];
  for (const name of order) {
    if (options.shouldStop && options.shouldStop()) return null;
    const file = await exportBrowserCookies(name, options);
    if (file) return file;
  }
  return null;
}

module.exports = {
  DEBUG_PORT,
  hasYouTubeSession,
  writeNetscapeCookies,
  listBrowserProfiles,
  readYoutubeCookiesFromDebugger,
  exportYoutubeCookies,
  encodeClientFrame,
  tryReadFrame
};
