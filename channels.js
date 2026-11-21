'use strict';

/**
 * channels.js — сбор ссылок на свежие короткие ролики с каналов YouTube.
 *
 * Видео не скачиваются. С каждого канала берутся N последних роликов.
 * Если ролик короче 2 минут, его ссылка дописывается в Links.txt.
 * Уже виденные ролики запоминаются в channel_history.json: если новых
 * видео нет, канал пропускается и ссылка повторно не пишется.
 */

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const {
  buildBaseYtDlpArgs,
  extractVideoId,
  killProcessTree,
  resolveYtDlpPath,
  ytdlpExists
} = require('./downloader');

const LINKS_FILE = 'Links.txt';
const HISTORY_FILE = 'channel_history.json';
const LOG_FILE = 'channel_log.txt';
const MAX_SHORT_SECONDS = 120;
const MAX_CHANNELS = 200;
const MAX_VIDEO_COUNT = 50;
const DEFAULT_VIDEO_COUNT = 1;
const HISTORY_VERSION = 1;

function normalizeVideoCount(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 1) return DEFAULT_VIDEO_COUNT;
  return Math.min(MAX_VIDEO_COUNT, n);
}

function normalizeChannelUrl(raw) {
  let text = String(raw || '').trim();
  if (!text) return null;
  if (/^@[\w.-]+$/i.test(text)) text = `https://www.youtube.com/${text}`;
  if (!/^https?:\/\//i.test(text)) {
    if (/^(www\.|m\.)?(youtube\.com|youtu\.be)\//i.test(text)) text = `https://${text}`;
    else return null;
  }
  let url;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  const host = url.hostname.replace(/^www\./, '').toLowerCase();
  if (host === 'youtu.be' || host === 'music.youtube.com') return null;
  if (host !== 'youtube.com' && host !== 'm.youtube.com') return null;
  const parts = url.pathname.split('/').filter(Boolean);
  if (!parts.length) return null;
  const head = parts[0];
  const tail = parts[1] || '';
  const tab = (parts[2] || '').toLowerCase();
  let pathname = null;
  if (head.startsWith('@')) {
    pathname = /^(videos|shorts|streams)$/i.test(tail) ? `/${head}/${tail.toLowerCase()}` : `/${head}/videos`;
  } else if ((head === 'channel' || head === 'c' || head === 'user') && tail) {
    pathname = /^(videos|shorts|streams)$/i.test(tab)
      ? `/${head}/${tail}/${tab.toLowerCase()}`
      : `/${head}/${tail}/videos`;
  }
  if (!pathname) return null;
  return `https://www.youtube.com${pathname}`;
}

function parseChannelList(text) {
  const urls = [];
  const seen = new Set();
  const tokens = String(text || '').split(/[\s,;]+/);
  tokens.forEach((token) => {
    const url = normalizeChannelUrl(token);
    if (!url) return;
    const key = url.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    urls.push(url);
  });
  return urls.slice(0, MAX_CHANNELS);
}

function readUtf8(file) {
  return fs.readFileSync(file, 'utf8');
}

function readHistory(file) {
  try {
    const parsed = JSON.parse(readUtf8(file));
    const channels = parsed && parsed.channels && typeof parsed.channels === 'object' ? parsed.channels : {};
    return { version: HISTORY_VERSION, channels };
  } catch {
    return { version: HISTORY_VERSION, channels: {} };
  }
}

function writeHistory(file, history) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify({ version: HISTORY_VERSION, channels: history.channels }, null, 2), 'utf8');
  try {
    fs.renameSync(tmp, file);
  } catch {
    fs.copyFileSync(tmp, file);
    fs.rmSync(tmp, { force: true });
  }
}

function readExistingLinkIds(file) {
  const ids = new Set();
  if (!file || !fs.existsSync(file)) return ids;
  readUtf8(file).split(/\r?\n/).forEach((line) => {
    const id = extractVideoId(line.trim());
    if (id) ids.add(id);
  });
  return ids;
}

function appendNewLinks(file, urls) {
  if (!urls.length) return;
  let current = '';
  try {
    if (fs.existsSync(file)) current = readUtf8(file);
  } catch {
    current = '';
  }
  const prefix = current.length && !current.endsWith('\n') ? '\n' : '';
  fs.appendFileSync(file, `${prefix}${urls.join('\n')}\n`, 'utf8');
}

function watchUrl(id) {
  return `https://www.youtube.com/watch?v=${id}`;
}

function videosFromPlaylist(info, limit) {
  const entries = info && Array.isArray(info.entries) ? info.entries : (info && info.id ? [info] : []);
  const videos = [];
  entries.forEach((entry) => {
    if (!entry || videos.length >= limit) return;
    const id = extractVideoId(entry.id) || extractVideoId(entry.url) || extractVideoId(entry.webpage_url);
    if (!id) return;
    const duration = entry.duration == null || entry.duration === '' ? null : Number(entry.duration);
    videos.push({
      id,
      url: watchUrl(id),
      title: entry.title || '',
      duration: Number.isFinite(duration) ? duration : null
    });
  });
  return videos;
}

function channelKeyFrom(info, inputUrl) {
  if (!info || typeof info !== 'object') return inputUrl;
  return info.channel_id || info.uploader_id || info.channel || info.id || inputUrl;
}

/**
 * latest — ролики от нового к старому.
 * ignored: среди них нет ни одного ещё не виденного.
 * Ссылка пишется только если длительность строго меньше maxSeconds.
 * Длинные ролики запоминаются, чтобы в следующий раз не считаться новыми.
 */
function classifyFresh(videos, seen, maxSeconds = MAX_SHORT_SECONDS) {
  const seenSet = seen instanceof Set ? seen : new Set(seen || []);
  const fresh = (videos || []).filter((video) => video && video.id && !seenSet.has(video.id));
  if (!fresh.length) return { ignored: true, links: [], markSeen: [], tooLong: 0, unknown: 0 };
  const links = [];
  const markSeen = [];
  let tooLong = 0;
  let unknown = 0;
  fresh.forEach((video) => {
    const duration = Number(video.duration);
    if (!Number.isFinite(duration) || duration <= 0) {
      unknown += 1;
      return;
    }
    markSeen.push(video.id);
    if (duration < maxSeconds) links.push(video.url);
    else tooLong += 1;
  });
  return { ignored: false, links, markSeen, tooLong, unknown };
}

class ChannelCollector {
  constructor(options = {}) {
    this.outputDir = options.outputDir;
    this.ytdlpPath = options.ytdlpPath || resolveYtDlpPath();
    this.runner = options.runner || null;
    this.hooks = options.hooks || {};
    this.count = normalizeVideoCount(options.count);
    this.maxSeconds = Number(options.maxSeconds) > 0 ? Number(options.maxSeconds) : MAX_SHORT_SECONDS;
    this.stopped = false;
    this.running = false;
    this.children = new Set();
  }

  log(level, message) {
    if (typeof this.hooks.onLog === 'function') this.hooks.onLog(level, message);
    if (!this.outputDir) return;
    try {
      const stamp = new Date().toTimeString().slice(0, 8);
      fs.appendFileSync(path.join(this.outputDir, LOG_FILE), `[${stamp}] ${message}\n`, 'utf8');
    } catch {
      /* лог не должен останавливать сбор */
    }
  }

  emit(summary, status) {
    if (typeof this.hooks.onProgress !== 'function') return;
    this.hooks.onProgress({ ...summary, status: status || null });
  }

  stop() {
    this.stopped = true;
    const children = Array.from(this.children);
    this.children.clear();
    children.forEach((child) => killProcessTree(child));
    this.log('warn', 'STOP — сбор ссылок остановлен');
  }

  async runYtDlp(args) {
    if (this.stopped) {
      const err = new Error('Сбор ссылок остановлен');
      err.cancelled = true;
      throw err;
    }
    if (this.runner) return this.runner(args);
    if (!ytdlpExists(this.ytdlpPath)) {
      throw new Error('yt-dlp не найден. Положите бинарник в vendor/yt-dlp или установите yt-dlp в PATH.');
    }
    return new Promise((resolve, reject) => {
      const child = spawn(this.ytdlpPath, args, {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: process.platform !== 'win32',
        env: {
          ...process.env,
          PYTHONUTF8: '1',
          PYTHONIOENCODING: 'utf-8'
        }
      });
      this.children.add(child);
      let stdout = '';
      let stderr = '';
      let settled = false;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearInterval(stopTimer);
        this.children.delete(child);
        fn(value);
      };
      const stopTimer = setInterval(() => {
        if (settled || !this.stopped) return;
        killProcessTree(child);
        const err = new Error('Сбор ссылок остановлен');
        err.cancelled = true;
        finish(reject, err);
      }, 150);
      child.stdout.on('data', (chunk) => {
        stdout += chunk.toString('utf8');
      });
      child.stderr.on('data', (chunk) => {
        stderr += chunk.toString('utf8');
      });
      child.on('error', (err) => finish(reject, err));
      child.on('close', (code) => {
        if (this.stopped) {
          const err = new Error('Сбор ссылок остановлен');
          err.cancelled = true;
          finish(reject, err);
          return;
        }
        if (code === 0) {
          finish(resolve, { stdout, stderr });
          return;
        }
        finish(reject, new Error((stderr || stdout || `yt-dlp exited ${code}`).trim().split('\n').slice(-6).join('\n')));
      });
    });
  }

  parseInfo(stdout) {
    const text = String(stdout || '');
    const start = text.indexOf('{');
    if (start < 0) throw new Error('yt-dlp не вернул данные канала');
    return JSON.parse(text.slice(start));
  }

  async probeVideo(url) {
    const { stdout } = await this.runYtDlp([
      ...buildBaseYtDlpArgs({ ytdlpPath: this.ytdlpPath }),
      '-J',
      '--no-warnings',
      '--skip-download',
      url
    ]);
    const info = this.parseInfo(stdout);
    const duration = info.duration == null ? null : Number(info.duration);
    return {
      duration: Number.isFinite(duration) ? duration : null,
      title: info.title || ''
    };
  }

  async listVideos(channelUrl) {
    const { stdout } = await this.runYtDlp([
      ...buildBaseYtDlpArgs({ ytdlpPath: this.ytdlpPath, playlist: true }),
      '--flat-playlist',
      '--playlist-end',
      String(this.count),
      '-J',
      '--no-warnings',
      channelUrl
    ]);
    const info = this.parseInfo(stdout);
    const videos = videosFromPlaylist(info, this.count);
    for (const video of videos) {
      if (this.stopped) break;
      if (video.duration != null) continue;
      const probed = await this.probeVideo(video.url);
      video.duration = probed.duration;
      if (probed.title) video.title = probed.title;
    }
    return { key: channelKeyFrom(info, channelUrl), videos };
  }

  async run(text) {
    if (this.running) throw new Error('Сбор ссылок уже запущен');
    if (!this.outputDir) throw new Error('Не выбрана папка для Links.txt.');
    const parsed = parseChannelList(text);
    if (!parsed.length) throw new Error('В списке нет ссылок на каналы YouTube.');
    const truncated = String(text || '').split(/[\s,;]+/).filter((token) => normalizeChannelUrl(token)).length > parsed.length;

    fs.mkdirSync(this.outputDir, { recursive: true });
    const linksPath = path.join(this.outputDir, LINKS_FILE);
    const historyPath = path.join(this.outputDir, HISTORY_FILE);
    if (!fs.existsSync(linksPath)) fs.writeFileSync(linksPath, '', 'utf8');
    if (!fs.existsSync(path.join(this.outputDir, LOG_FILE))) {
      fs.writeFileSync(path.join(this.outputDir, LOG_FILE), '', 'utf8');
    }
    const history = readHistory(historyPath);
    const existingIds = readExistingLinkIds(linksPath);

    this.running = true;
    this.stopped = false;
    const summary = {
      channels: parsed.length,
      checked: 0,
      ignored: 0,
      links: 0,
      tooLong: 0,
      unknown: 0,
      errors: 0,
      cancelled: false,
      linksFile: linksPath
    };

    this.log('info', `Каналов: ${parsed.length}, последних видео с канала: ${this.count}, короче ${this.maxSeconds} с`);
    if (truncated) this.log('warn', `Беру первые ${MAX_CHANNELS} каналов, остальные строки пропущены.`);
    this.emit(summary, 'Сбор ссылок');

    try {
      for (const channelUrl of parsed) {
        if (this.stopped) break;
        this.log('info', `Канал: ${channelUrl}`);
        try {
          const listed = await this.listVideos(channelUrl);
          if (this.stopped) break;
          const key = listed.key || channelUrl;
          const record = history.channels[key] && typeof history.channels[key] === 'object'
            ? history.channels[key]
            : { input: channelUrl, seen: [] };
          const seen = new Set(Array.isArray(record.seen) ? record.seen : []);
          const decision = classifyFresh(listed.videos, seen, this.maxSeconds);
          summary.checked += 1;
          if (decision.ignored) {
            summary.ignored += 1;
            this.log('info', `Новых видео нет — канал пропущен: ${channelUrl}`);
            this.emit(summary);
            continue;
          }
          decision.markSeen.forEach((id) => seen.add(id));
          const freshLinks = decision.links.filter((url) => {
            const id = extractVideoId(url);
            return id && !existingIds.has(id);
          });
          freshLinks.forEach((url) => existingIds.add(extractVideoId(url)));
          summary.links += freshLinks.length;
          summary.tooLong += decision.tooLong;
          summary.unknown += decision.unknown;
          if (freshLinks.length) {
            appendNewLinks(linksPath, freshLinks);
            this.log('info', `В Links.txt добавлено ${freshLinks.length}: ${channelUrl}`);
          }
          record.input = channelUrl;
          record.seen = Array.from(seen).slice(-5000);
          record.updatedAt = new Date().toISOString();
          history.channels[key] = record;
          writeHistory(historyPath, history);
          if (!freshLinks.length && decision.tooLong && !decision.links.length) {
            this.log('info', `Новые ролики длиннее 2 минут — ссылка не записана: ${channelUrl}`);
          } else if (!freshLinks.length && decision.unknown && !decision.links.length) {
            this.log('warn', `Длительность новых роликов неизвестна — ссылка не записана: ${channelUrl}`);
          } else if (!freshLinks.length) {
            this.log('info', `Новых коротких роликов нет: ${channelUrl}`);
          }
          this.emit(summary);
        } catch (err) {
          if ((err && err.cancelled) || this.stopped) {
            this.stopped = true;
            break;
          }
          summary.errors += 1;
          this.log('error', `${channelUrl}: ${(err && err.message) || err}`);
          this.emit(summary);
        }
      }
      summary.cancelled = this.stopped;
      this.log(
        summary.cancelled ? 'warn' : 'success',
        summary.cancelled
          ? `Сбор остановлен. Записано ссылок: ${summary.links}`
          : `Готово. Ссылок: ${summary.links}, без новых видео: ${summary.ignored}, длиннее 2 минут: ${summary.tooLong}`
      );
      this.emit(summary, summary.cancelled ? 'Остановлено' : 'Готово');
      return summary;
    } finally {
      this.running = false;
    }
  }
}

module.exports = {
  ChannelCollector,
  LINKS_FILE,
  HISTORY_FILE,
  LOG_FILE,
  MAX_SHORT_SECONDS,
  MAX_CHANNELS,
  MAX_VIDEO_COUNT,
  DEFAULT_VIDEO_COUNT,
  normalizeVideoCount,
  normalizeChannelUrl,
  parseChannelList,
  classifyFresh,
  videosFromPlaylist,
  channelKeyFrom,
  readHistory,
  readExistingLinkIds
};
