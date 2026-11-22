'use strict';

/**
 * channels.js — сбор ссылок на свежие короткие ролики с каналов YouTube.
 *
 * Видео не скачиваются. С каждого канала берётся вкладка Shorts и ролики с указанной даты.
 * Если ролик не длиннее 2,5 минут (150 с), его ссылка дописывается в Links.txt.
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
  ytdlpExists,
  isBotCheckError,
  isCookieError,
  cookieBrowserCandidates,
  shortYtError,
  easeChildProcess,
  YOUTUBE_EXTRACTOR_ARGS
} = require('./downloader');

const LINKS_FILE = 'Links.txt';
const HISTORY_FILE = 'channel_history.json';
const LOG_FILE = 'channel_log.txt';
const MAX_SHORT_SECONDS = 150;
const MIN_SHORT_SECONDS = 35;
const MAX_CHANNELS = 200;
const MAX_VIDEO_COUNT = 50;
const MAX_SINCE_VIDEOS = 400;
const DEFAULT_VIDEO_COUNT = 1;
const DEFAULT_CHANNEL_CONCURRENCY = 2;
const MAX_CHANNEL_CONCURRENCY = 6;
const PROBE_CONCURRENCY = 2;
/** Больше этого числа роликов без длительности по одному не проверяем: иначе сбор замирает. */
const MAX_INLINE_PROBES = 8;
/** Один зависший yt-dlp не должен держать весь сбор. */
const PLAYLIST_TIMEOUT_MS = 50_000;
/** Сколько роликов подряд старше даты нужно увидеть, чтобы остановить просмотр списка. */
const OLD_STREAK_TO_STOP = 12;
const HISTORY_VERSION = 1;

function normalizeVideoCount(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 1) return DEFAULT_VIDEO_COUNT;
  return Math.min(MAX_VIDEO_COUNT, n);
}

function normalizeChannelConcurrency(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 1) return DEFAULT_CHANNEL_CONCURRENCY;
  return Math.min(MAX_CHANNEL_CONCURRENCY, n);
}

/** Дата «с какого дня» в виде YYYYMMDD. Принимает YYYY-MM-DD, YYYYMMDD и ДД.ММ.ГГГГ. */
function normalizeSinceDate(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  let year;
  let month;
  let day;
  let match = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (match) {
    year = Number(match[1]);
    month = Number(match[2]);
    day = Number(match[3]);
  } else if ((match = text.match(/^(\d{4})(\d{2})(\d{2})$/))) {
    year = Number(match[1]);
    month = Number(match[2]);
    day = Number(match[3]);
  } else if ((match = text.match(/^(\d{2})\.(\d{2})\.(\d{4})$/))) {
    day = Number(match[1]);
    month = Number(match[2]);
    year = Number(match[3]);
  } else {
    return null;
  }
  const dt = new Date(Date.UTC(year, month - 1, day));
  if (dt.getUTCFullYear() !== year || dt.getUTCMonth() !== month - 1 || dt.getUTCDate() !== day) return null;
  const mm = String(month).padStart(2, '0');
  const dd = String(day).padStart(2, '0');
  return `${year}${mm}${dd}`;
}

function formatSinceDate(ymd) {
  const text = String(ymd || '');
  if (!/^\d{8}$/.test(text)) return text;
  return `${text.slice(6, 8)}.${text.slice(4, 6)}.${text.slice(0, 4)}`;
}

function durationLimitLabel(seconds) {
  const n = Number(seconds);
  if (n === 150) return '2,5 минут';
  if (n > 0 && n % 60 === 0) return `${n / 60} мин`;
  return `${n} с`;
}

function uploadDateFrom(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const raw = entry.upload_date || entry.release_date;
  if (/^\d{8}$/.test(String(raw || ''))) return String(raw);
  const ts = Number(entry.timestamp || entry.release_timestamp);
  if (!Number.isFinite(ts) || ts <= 0) return null;
  const dt = new Date(ts * 1000);
  const year = dt.getUTCFullYear();
  const month = String(dt.getUTCMonth() + 1).padStart(2, '0');
  const day = String(dt.getUTCDate()).padStart(2, '0');
  return `${year}${month}${day}`;
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
  let pathname = null;
  if (head.startsWith('@')) {
    pathname = `/${head}/shorts`;
  } else if ((head === 'channel' || head === 'c' || head === 'user') && tail && !/^(videos|shorts|streams|featured|about|playlists|community|posts)$/i.test(tail)) {
    pathname = `/${head}/${tail}/shorts`;
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

function durationSeconds(entry) {
  if (!entry || typeof entry !== 'object') return null;
  if (entry.duration != null && entry.duration !== '') {
    const n = Number(entry.duration);
    if (Number.isFinite(n) && n > 0) return n;
  }
  const parts = String(entry.duration_string || '').trim().split(':').map((part) => Number(part));
  if (parts.length >= 2 && parts.length <= 3 && parts.every((part) => Number.isFinite(part))) {
    return parts.reduce((total, part) => total * 60 + part, 0);
  }
  return null;
}

/** Полный список Shorts канала: UC… → плейлист UUSH…. Вкладка /shorts часто отдаёт только первые ролики. */
function knownDurationCount(videos) {
  return (videos || []).reduce((count, video) => count + (video && video.duration != null ? 1 : 0), 0);
}

/** Берём список, в котором больше известных длительностей, а при равенстве — более длинный. */
function preferRicherList(current, incoming) {
  if (!incoming || !incoming.length) return current || [];
  if (!current || !current.length) return incoming;
  const currentKnown = knownDurationCount(current);
  const incomingKnown = knownDurationCount(incoming);
  if (incomingKnown > currentKnown) return incoming;
  if (incomingKnown === currentKnown && incoming.length > current.length) return incoming;
  return current;
}

function shortsPlaylistUrl(channelId) {
  const id = String(channelId || '');
  if (!/^UC[A-Za-z0-9_-]{2,}$/.test(id)) return null;
  return `https://www.youtube.com/playlist?list=UUSH${id.slice(2)}`;
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
    videos.push({
      id,
      url: watchUrl(id),
      title: entry.title || '',
      duration: durationSeconds(entry),
      uploadDate: uploadDateFrom(entry)
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
 * Ссылка пишется, если длительность от minSeconds до maxSeconds включительно.
 * Короткие и длинные ролики запоминаются, чтобы в следующий раз не считаться новыми.
 */
function classifyFresh(videos, seen, maxSeconds = MAX_SHORT_SECONDS, minSeconds = MIN_SHORT_SECONDS) {
  const seenSet = seen instanceof Set ? seen : new Set(seen || []);
  const fresh = (videos || []).filter((video) => video && video.id && !seenSet.has(video.id));
  if (!fresh.length) return { ignored: true, links: [], markSeen: [], tooLong: 0, tooShort: 0, unknown: 0 };
  const links = [];
  const markSeen = [];
  let tooLong = 0;
  let tooShort = 0;
  let unknown = 0;
  const floor = Number(minSeconds) > 0 ? Number(minSeconds) : MIN_SHORT_SECONDS;
  fresh.forEach((video) => {
    const duration = Number(video.duration);
    if (!Number.isFinite(duration) || duration <= 0) {
      unknown += 1;
      return;
    }
    markSeen.push(video.id);
    if (duration < floor) tooShort += 1;
    else if (duration <= maxSeconds) links.push(video.url);
    else tooLong += 1;
  });
  return { ignored: false, links, markSeen, tooLong, tooShort, unknown };
}

class ChannelCollector {
  constructor(options = {}) {
    this.outputDir = options.outputDir;
    this.ytdlpPath = options.ytdlpPath || resolveYtDlpPath();
    this.runner = options.runner || null;
    this.hooks = options.hooks || {};
    this.count = normalizeVideoCount(options.count);
    this.since = normalizeSinceDate(options.since);
    this.maxSeconds = Number(options.maxSeconds) > 0 ? Number(options.maxSeconds) : MAX_SHORT_SECONDS;
    this.minSeconds = Number(options.minSeconds) > 0 ? Number(options.minSeconds) : MIN_SHORT_SECONDS;
    this.concurrency = normalizeChannelConcurrency(options.concurrency);
    this.stopped = false;
    this.running = false;
    this.children = new Set();
    this._lock = Promise.resolve();
    this._probeActive = 0;
    this._probeWaiters = [];
    this.cookiesFromBrowser = null;
  }

  async locked(fn) {
    const previous = this._lock;
    let release;
    this._lock = new Promise((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await fn();
    } finally {
      release();
    }
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
      easeChildProcess(child);
      let stdout = '';
      let stderr = '';
      let settled = false;
      let deadline;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearInterval(stopTimer);
        clearTimeout(deadline);
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
      const deadlineTimer = setTimeout(() => {
        if (settled) return;
        killProcessTree(child);
        finish(reject, new Error(`yt-dlp не ответил за ${Math.round(PLAYLIST_TIMEOUT_MS / 1000)} с`));
      }, PLAYLIST_TIMEOUT_MS);
      deadline = deadlineTimer;
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
      title: info.title || '',
      uploadDate: uploadDateFrom(info)
    };
  }

  async withProbeSlot(fn) {
    while (this._probeActive >= PROBE_CONCURRENCY) {
      await new Promise((resolve) => {
        this._probeWaiters.push(resolve);
      });
      if (this.stopped) return undefined;
    }
    this._probeActive += 1;
    try {
      return await fn();
    } finally {
      this._probeActive -= 1;
      const next = this._probeWaiters.shift();
      if (next) next();
    }
  }

  playlistArgs(url, limit, cookiesFromBrowser) {
    return [
      ...buildBaseYtDlpArgs({
        ytdlpPath: this.ytdlpPath,
        playlist: true,
        cookiesFromBrowser,
        extractorArgs: `${YOUTUBE_EXTRACTOR_ARGS};youtubetab:approximate_date`
      }),
      '--flat-playlist',
      '--playlist-end',
      String(limit),
      '-J',
      '--no-warnings',
      url
    ];
  }

  async fetchPlaylist(url, limit) {
    const attempt = async (cookiesFromBrowser) => {
      const { stdout } = await this.runYtDlp(this.playlistArgs(url, limit, cookiesFromBrowser));
      return this.parseInfo(stdout);
    };
    const first = this.cookiesFromBrowser || null;
    try {
      return await attempt(first);
    } catch (err) {
      if ((err && err.cancelled) || this.stopped) throw err;
      const message = (err && err.message) || '';
      const retry = (!first && isBotCheckError(message)) || (first && (isBotCheckError(message) || isCookieError(message)));
      if (!retry) throw err;
      let lastError = err;
      for (const name of cookieBrowserCandidates()) {
        if (this.stopped) throw err;
        if (name === first) continue;
        try {
          this.log('warn', `YouTube просит вход — повторяю список с cookies из ${name}`);
          const info = await attempt(name);
          this.cookiesFromBrowser = name;
          return info;
        } catch (nextErr) {
          if ((nextErr && nextErr.cancelled) || this.stopped) throw nextErr;
          lastError = nextErr;
          if (isBotCheckError(nextErr && nextErr.message) || isCookieError(nextErr && nextErr.message)) continue;
          throw nextErr;
        }
      }
      throw lastError;
    }
  }

  async probeMissing(videos) {
    const pending = videos.filter((video) => video.duration == null);
    for (let offset = 0; offset < pending.length && !this.stopped; offset += PROBE_CONCURRENCY) {
      const batch = pending.slice(offset, offset + PROBE_CONCURRENCY);
      await Promise.all(batch.map((video) => this.withProbeSlot(async () => {
        if (this.stopped || video.duration != null) return;
        try {
          const probed = await this.probeVideo(video.url);
          if (video.duration == null) video.duration = probed.duration;
          if (!video.uploadDate && probed.uploadDate) video.uploadDate = probed.uploadDate;
          if (probed.title) video.title = probed.title;
        } catch (err) {
          if ((err && err.cancelled) || this.stopped) throw err;
        }
      })));
    }
  }

  async listVideos(channelUrl) {
    const limit = this.since ? MAX_SINCE_VIDEOS : this.count;
    const info = await this.fetchPlaylist(channelUrl, limit);
    let videos = videosFromPlaylist(info, limit);
    const missingDurations = videos.length - knownDurationCount(videos);
    const playlistUrl = shortsPlaylistUrl(channelKeyFrom(info, ''));
    if (playlistUrl && (missingDurations > MAX_INLINE_PROBES || (this.since && videos.length < limit))) {
      try {
        const fullInfo = await this.fetchPlaylist(playlistUrl, limit);
        videos = preferRicherList(videos, videosFromPlaylist(fullInfo, limit));
      } catch (err) {
        if ((err && err.cancelled) || this.stopped) throw err;
        this.log('warn', `Полный список Shorts не открылся, беру вкладку канала: ${shortYtError(err && err.message)}`);
      }
    }
    const selected = [];
    let reachedOlder = false;
    let oldStreak = 0;
    videos.forEach((video) => {
      if (this.stopped || reachedOlder) return;
      if (this.since && video.uploadDate && video.uploadDate < this.since) {
        oldStreak += 1;
        if (oldStreak >= OLD_STREAK_TO_STOP) reachedOlder = true;
        return;
      }
      oldStreak = 0;
      selected.push(video);
    });
    if (oldStreak > 0) reachedOlder = true;
    if (this.since && !reachedOlder && videos.length >= limit) {
      this.log(
        'warn',
        `С ${formatSinceDate(this.since)} взяты первые ${limit} роликов, более старые не проверялись: ${channelUrl}`
      );
    }
    const pending = selected.filter((video) => video.duration == null);
    if (pending.length > MAX_INLINE_PROBES) {
      this.log(
        'warn',
        `У ${pending.length} Shorts нет длительности в списке — по одному их не проверяю, чтобы сбор не завис: ${channelUrl}`
      );
    } else if (pending.length) {
      await this.probeMissing(pending);
    }
    if (this.since) {
      for (let index = selected.length - 1; index >= 0; index -= 1) {
        const video = selected[index];
        if (video.uploadDate && video.uploadDate < this.since) selected.splice(index, 1);
      }
    }
    this.log('info', `Shorts в списке: ${videos.length}, с выбранного окна: ${selected.length}: ${channelUrl}`);
    return { key: channelKeyFrom(info, channelUrl), videos: selected, reachedOlder };
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
      tooShort: 0,
      unknown: 0,
      errors: 0,
      cancelled: false,
      linksFile: linksPath
    };

    const windowText = this.since
      ? `Shorts с ${formatSinceDate(this.since)}`
      : `последних Shorts с канала: ${this.count}`;
    this.log(
      'info',
      `Каналов: ${parsed.length}, ${windowText}, от ${this.minSeconds} до ${this.maxSeconds} с, потоков: ${this.concurrency}`
    );
    if (truncated) this.log('warn', `Беру первые ${MAX_CHANNELS} каналов, остальные строки пропущены.`);
    this.emit(summary, 'Сбор ссылок');

    try {
      let cursor = 0;
      const worker = async () => {
        while (!this.stopped) {
          const index = cursor;
          cursor += 1;
          if (index >= parsed.length) return;
          const channelUrl = parsed[index];
          this.log('info', `Канал: ${channelUrl}`);
          try {
            const listed = await this.listVideos(channelUrl);
            if (this.stopped) return;
            await this.locked(async () => {
              if (this.stopped) return;
              const key = listed.key || channelUrl;
              const record = history.channels[key] && typeof history.channels[key] === 'object'
                ? history.channels[key]
                : { input: channelUrl, seen: [] };
              const seen = new Set(Array.isArray(record.seen) ? record.seen : []);
              if (!listed.videos.length) {
                summary.checked += 1;
                summary.ignored += 1;
                this.log(
                  'info',
                  listed.reachedOlder
                    ? `На вкладке Shorts нет роликов с ${formatSinceDate(this.since)}: ${channelUrl}`
                    : `На вкладке Shorts нет роликов: ${channelUrl}`
                );
                this.emit(summary);
                return;
              }
              const decision = classifyFresh(listed.videos, seen, this.maxSeconds, this.minSeconds);
              summary.checked += 1;
              if (decision.ignored) {
                summary.ignored += 1;
                this.log('info', `Новых Shorts нет — канал пропущен: ${channelUrl}`);
                this.emit(summary);
                return;
              }
              decision.markSeen.forEach((id) => seen.add(id));
              const freshLinks = decision.links.filter((url) => {
                const id = extractVideoId(url);
                return id && !existingIds.has(id);
              });
              freshLinks.forEach((url) => existingIds.add(extractVideoId(url)));
              summary.links += freshLinks.length;
              summary.tooLong += decision.tooLong;
              summary.tooShort += decision.tooShort;
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
              if (!freshLinks.length && decision.tooShort && !decision.tooLong) {
                this.log('info', `Новые Shorts короче ${this.minSeconds} с — ссылка не записана: ${channelUrl}`);
              } else if (!freshLinks.length && decision.tooLong && !decision.tooShort) {
                this.log('info', `Новые ролики длиннее ${durationLimitLabel(this.maxSeconds)} — ссылка не записана: ${channelUrl}`);
              } else if (!freshLinks.length && decision.tooShort && decision.tooLong) {
                this.log('info', `Новые Shorts короче ${this.minSeconds} с или длиннее ${durationLimitLabel(this.maxSeconds)} — ссылка не записана: ${channelUrl}`);
              } else if (!freshLinks.length && decision.unknown) {
                this.log('warn', `Длительность новых роликов неизвестна — ссылка не записана: ${channelUrl}`);
              } else if (!freshLinks.length) {
                this.log('info', `Новых коротких роликов нет: ${channelUrl}`);
              }
              this.emit(summary);
            });
          } catch (err) {
            if ((err && err.cancelled) || this.stopped) {
              this.stopped = true;
              return;
            }
            await this.locked(async () => {
              summary.errors += 1;
              this.log('error', `${channelUrl}: ${(err && err.message) || err}`);
              this.emit(summary);
            });
          }
        }
      };
      const workers = Math.min(this.concurrency, parsed.length);
      await Promise.all(Array.from({ length: workers }, () => worker()));
      summary.cancelled = this.stopped;
      this.log(
        summary.cancelled ? 'warn' : 'success',
        summary.cancelled
          ? `Сбор остановлен. Записано ссылок: ${summary.links}`
          : `Готово. Ссылок: ${summary.links}, без новых видео: ${summary.ignored}, короче ${this.minSeconds} с: ${summary.tooShort}, длиннее ${durationLimitLabel(this.maxSeconds)}: ${summary.tooLong}`
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
  MIN_SHORT_SECONDS,
  MAX_CHANNELS,
  MAX_VIDEO_COUNT,
  MAX_SINCE_VIDEOS,
  DEFAULT_VIDEO_COUNT,
  DEFAULT_CHANNEL_CONCURRENCY,
  PROBE_CONCURRENCY,
  MAX_INLINE_PROBES,
  knownDurationCount,
  preferRicherList,
  shortsPlaylistUrl,
  normalizeVideoCount,
  normalizeChannelConcurrency,
  normalizeSinceDate,
  formatSinceDate,
  normalizeChannelUrl,
  parseChannelList,
  classifyFresh,
  videosFromPlaylist,
  channelKeyFrom,
  readHistory,
  readExistingLinkIds
};
