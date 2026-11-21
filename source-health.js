'use strict';

/**
 * source-health.js — диагностика и восстановление входных видео перед рендером.
 *
 * Зачем: H.264/HEVC в MP4/MOV хранится в формате AVCC/HVCC — каждый NAL-блок
 * пакета начинается с длины. Если байты пакета повреждены (недокачанный или
 * криво склеенный файл, «дыры» после докачки), декодер пишет
 *   Invalid NAL unit size / Error splitting the input into NAL units
 * и при доле ошибок > 2/3 ffmpeg завершает рендер с кодом 69 (или ждёт кадры,
 * которых не будет). Видеокарта тут ни при чём, поэтому повтор на CPU с тем же
 * входом даёт ту же ошибку.
 *
 *   scanPackets()           — быстрая проверка без декодирования: список пакетов
 *                             (ffprobe) + проверка длин NAL прямо в байтах файла,
 *                             метки времени, ключевые кадры. Кэш по версии файла;
 *   probeDecode()           — точная проверка декодированием нужного окна;
 *   classifyFfmpegFailure() — отличает ошибку декодера входа от ошибки GPU;
 *   ensureInputHealthy()    — решение: быстрый путь / предупреждение /
 *                             нормализованная копия (только для повреждённых
 *                             источников, с кэшем) / понятная ошибка с файлом и
 *                             временным участком.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

/** Сколько секунд нечитаемого видео в одном окне допускается закрыть соседними кадрами. */
const MAX_CONCEALED_SECONDS = 1.0;
/** Доля ошибок в окне, при которой ffmpeg уже рискует упасть (его лимит — 2/3). */
const RISKY_ERROR_SHARE = 0.25;
/** PSNR ниже этого после повреждения — кадры копии сдвинуты относительно исходника. */
const MIN_ALIGNMENT_PSNR = 36;
/** Версия алгоритма восстановления: меняется — старый кэш не используется. */
const REPAIR_VERSION = 2;
const AVCC_CONTAINERS = /(mov|mp4|m4a|3gp|3g2|mj2|ismv|isom)/i;

class SourceDecodeError extends Error {
  /** Ошибка декодирования конкретного входа при рендере (не GPU). */
  constructor(cause, diagnosis, inputs) {
    const list = (diagnosis.inputIndexes || [])
      .map((i) => (inputs && inputs[i] ? `#${i} ${path.basename(inputs[i].file)}` : `#${i}`))
      .join(', ');
    super(
      `FFmpeg не смог декодировать входной файл${list ? ` (${list})` : ''}: ` +
        `${(diagnosis.lines[0] || '').trim() || (cause && cause.message) || 'ошибка декодера'}`
    );
    this.name = 'SourceDecodeError';
    this.sourceDecode = true;
    this.diagnosis = diagnosis;
    this.renderInputs = inputs || [];
    this.ffmpegStderr = cause && cause.ffmpegStderr;
    this.ffmpegCommand = cause && cause.ffmpegCommand;
    this.exitMessage = cause && cause.message;
  }
}

class SourceCorruptError extends Error {
  /** Источник повреждён так, что безопасно восстановить нужный участок нельзя. */
  constructor(message, details = {}) {
    super(message);
    this.name = 'SourceCorruptError';
    this.sourceCorrupt = true;
    this.details = details;
  }
}

function clock(seconds) {
  const ms = Math.round((Number.isFinite(seconds) && seconds > 0 ? seconds : 0) * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const mmss = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
  return h ? `${h}:${mmss}` : mmss;
}

function rangeText(ranges) {
  return ranges.map((r) => `${clock(r.start)}–${clock(r.end)}`).join(', ');
}

function versionKey(file) {
  const st = fs.statSync(file);
  return `${path.resolve(file)}|${st.size}|${Math.round(st.mtimeMs)}`;
}

/** Запуск процесса; stdout можно разбирать построчно (onLine), не копя в памяти. */
function runProcess(bin, args, { onLine, onSpawn, timeoutMs = 0 } = {}) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(bin, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err) {
      reject(err);
      return;
    }
    if (typeof onSpawn === 'function') onSpawn(child);
    let stdout = '';
    let stderr = '';
    let timer = null;
    if (timeoutMs > 0) timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (!onLine) {
        if (stdout.length < 64 * 1024 * 1024) stdout += chunk;
        return;
      }
      stdout += chunk;
      const lines = stdout.split(/\r?\n/);
      stdout = lines.pop();
      lines.forEach((line) => line && onLine(line));
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk) => {
      if (stderr.length < 4 * 1024 * 1024) stderr += chunk;
    });
    child.on('error', (err) => {
      if (timer) clearTimeout(timer);
      reject(err);
    });
    child.on('close', (code, signal) => {
      if (timer) clearTimeout(timer);
      if (onLine && stdout) onLine(stdout);
      resolve({ code, signal, stdout: onLine ? '' : stdout, stderr });
    });
  });
}

function parseCompact(line) {
  const out = {};
  line.split('|').forEach((part) => {
    const eq = part.indexOf('=');
    if (eq > 0) out[part.slice(0, eq)] = part.slice(eq + 1);
  });
  return out;
}

function num(value) {
  const n = Number(value);
  return value !== '' && value != null && Number.isFinite(n) ? n : null;
}

/** ffprobe -show_data печатает extradata hex-дампом: "00000000: 0164 001f ffe1 ...  .d....". */
function parseHexDump(text) {
  const bytes = [];
  String(text || '').split('\n').forEach((line) => {
    const m = line.match(/^[0-9a-f]{8}:\s+((?:[0-9a-f]{2,4}\s?)+)/i);
    if (!m) return;
    m[1].trim().split(/\s+/).forEach((group) => {
      for (let i = 0; i + 1 < group.length; i += 2) bytes.push(parseInt(group.slice(i, i + 2), 16));
    });
  });
  return Buffer.from(bytes);
}

/** Длина префикса NAL из avcC/hvcC. null — поток в Annex B (стартовые коды): длины не проверяем. */
function nalLengthSize(codec, extradata) {
  if (!extradata || extradata.length < 7 || extradata[0] !== 1) return null;
  if (codec === 'h264' && extradata.length >= 5) return (extradata[4] & 0x03) + 1;
  if (codec === 'hevc' && extradata.length >= 23) return (extradata[21] & 0x03) + 1;
  return null;
}

/**
 * Структура пакета: цепочка «длина + NAL» должна ровно покрыть пакет, у каждого
 * NAL — корректный заголовок. Читаются только заголовки, не весь пакет.
 */
function checkPacketNal(fd, pos, size, lengthSize, codec, scratch) {
  if (!(size > 0)) return 'пустой пакет';
  let offset = 0;
  let nals = 0;
  while (offset < size) {
    if (size - offset < lengthSize + 1) return `обрезанный NAL (${size - offset} байт в хвосте)`;
    const got = fs.readSync(fd, scratch, 0, Math.min(lengthSize + 2, size - offset), pos + offset);
    if (got < lengthSize + 1) return 'пакет за пределами файла';
    let len = 0;
    for (let i = 0; i < lengthSize; i += 1) len = len * 256 + scratch[i];
    if (len === 0) return 'NAL нулевой длины (нули вместо данных)';
    if (len > size - offset - lengthSize) return `Invalid NAL unit size (${len} > ${size - offset - lengthSize})`;
    const header = scratch[lengthSize];
    if (header & 0x80) return 'запрещённый бит в заголовке NAL';
    if (codec === 'h264' && (header & 0x1f) === 0) return 'NAL типа 0';
    offset += lengthSize + len;
    nals += 1;
    if (nals > 4096) return 'слишком много NAL в пакете';
  }
  return null;
}

/** Слить плохие пакеты в участки и продлить каждый до следующего ключевого кадра. */
function buildBadRanges(bad, keyframes, frameDur, videoEnd) {
  const ranges = [];
  bad.slice().sort((a, b) => a.t - b.t).forEach((item) => {
    const last = ranges[ranges.length - 1];
    if (last && item.t - last.end <= 1.0) {
      last.end = item.t + frameDur;
      last.packets += 1;
    } else {
      ranges.push({ start: item.t, end: item.t + frameDur, packets: 1, reason: item.reason });
    }
  });
  ranges.forEach((range) => {
    const nextKey = keyframes.find((t) => t >= range.end - 1e-6);
    range.affectedEnd = Number.isFinite(nextKey) ? nextKey : videoEnd;
  });
  return ranges;
}

const scanCache = new Map();

/** Быстрая проверка файла без декодирования. Кэш по пути, размеру и mtime. */
function scanPackets(media, { ffprobePath, onSpawn } = {}) {
  let key;
  try {
    key = versionKey(media.file);
  } catch (err) {
    return Promise.reject(new SourceCorruptError(`Источник недоступен: ${media.file} (${err.message})`, { file: media.file }));
  }
  if (scanCache.has(key)) return scanCache.get(key);
  const job = scanPacketsUncached(media, { ffprobePath, onSpawn }).catch((err) => {
    scanCache.delete(key);
    throw err;
  });
  scanCache.set(key, job);
  return job;
}

async function scanPacketsUncached(media, { ffprobePath, onSpawn }) {
  const file = media.file;
  const streamSel = Number.isInteger(media.videoIndex) ? String(media.videoIndex) : 'v:0';
  const head = await runProcess(ffprobePath, [
    '-v', 'error', '-show_data', '-select_streams', streamSel,
    '-show_entries', 'stream=index,codec_name,start_time,r_frame_rate,avg_frame_rate,duration,nb_frames,extradata:format=format_name,start_time,duration',
    '-of', 'json', file
  ], { onSpawn, timeoutMs: 60000 });
  let info = {};
  try {
    info = JSON.parse(head.stdout || '{}');
  } catch (err) {
    info = {};
  }
  const stream = (info.streams || [])[0];
  if (!stream) throw new SourceCorruptError(`В файле нет читаемого видеопотока: ${path.basename(file)}`, { file });
  const format = info.format || {};
  const codec = String(stream.codec_name || '');
  const container = String(format.format_name || '');
  const baseTime = num(format.start_time) || 0;
  const lengthSize = AVCC_CONTAINERS.test(container) && (codec === 'h264' || codec === 'hevc')
    ? nalLengthSize(codec, parseHexDump(stream.extradata))
    : null;
  const fps = media.fps > 0 ? media.fps : 30;
  const frameDur = 1 / fps;

  const packets = [];
  const result = await runProcess(ffprobePath, [
    '-v', 'error', '-select_streams', streamSel,
    '-show_entries', 'packet=pts_time,dts_time,size,pos,flags', '-of', 'compact=p=0', file
  ], {
    onSpawn,
    onLine: (line) => {
      const p = parseCompact(line);
      packets.push({
        pts: num(p.pts_time),
        dts: num(p.dts_time),
        size: num(p.size) || 0,
        pos: num(p.pos),
        key: String(p.flags || '').includes('K')
      });
    }
  });

  const bad = [];
  const tsIssues = [];
  const keyframes = [];
  let lastDts = -Infinity;
  let videoEnd = 0;
  let fd = null;
  const scratch = Buffer.alloc(16);
  try {
    if (lengthSize) fd = fs.openSync(file, 'r');
    for (let i = 0; i < packets.length; i += 1) {
      const p = packets[i];
      const t = (p.pts != null ? p.pts : p.dts != null ? p.dts : NaN) - baseTime;
      if (!Number.isFinite(t)) {
        tsIssues.push({ t: Number.isFinite(lastDts) ? lastDts - baseTime : 0, reason: 'пакет без меток времени' });
        continue;
      }
      if (p.dts != null) {
        if (p.dts < lastDts - 1e-6) tsIssues.push({ t, reason: 'DTS идёт назад' });
        else if (Number.isFinite(lastDts) && p.dts - lastDts > Math.max(2, frameDur * 30)) {
          tsIssues.push({ t, reason: `разрыв меток времени ${(p.dts - lastDts).toFixed(2)} с` });
        }
        lastDts = Math.max(lastDts, p.dts);
      }
      if (p.key) keyframes.push(t);
      videoEnd = Math.max(videoEnd, t + frameDur);
      if (fd != null && p.pos != null && p.pos >= 0) {
        const reason = checkPacketNal(fd, p.pos, p.size, lengthSize, codec, scratch);
        if (reason) bad.push({ t, reason });
      } else if (!(p.size > 0)) {
        bad.push({ t, reason: 'пустой пакет' });
      }
      // Не блокируем главный процесс Electron на длинных файлах.
      if (i % 4000 === 3999) await new Promise((resolve) => setImmediate(resolve));
    }
  } finally {
    if (fd != null) fs.closeSync(fd);
  }
  keyframes.sort((a, b) => a - b);
  return {
    file,
    codec,
    container,
    nalLengthSize: lengthSize,
    rFrameRate: /^\d+\/[1-9]\d*$/.test(String(stream.r_frame_rate || '')) ? stream.r_frame_rate : null,
    structureChecked: Boolean(lengthSize),
    packets: packets.length,
    keyframes,
    videoEnd,
    frameDur,
    badPackets: bad.length,
    badRanges: buildBadRanges(bad, keyframes, frameDur, videoEnd),
    tsIssues: tsIssues.slice(0, 50),
    demuxErrors: String(result.stderr || '').split(/\r?\n/).filter(Boolean).slice(0, 20)
  };
}

/** Участки скана, задевающие окно [start, start + duration). */
function rangesInWindow(scan, start, duration) {
  const end = start + duration;
  return (scan.badRanges || []).filter((r) => r.start < end && r.affectedEnd > start);
}

const DECODE_ERROR_RE =
  /(Invalid NAL unit size|Error splitting the input into NAL units|Decode error rate|Error while decoding stream|Decoding error|Error submitting packet to decoder|corrupted macroblock|concealing \d+ DC|error while decoding MB|Invalid data found when processing input|missing picture in access unit|no frame!|non-existing PPS|decode_slice_header error|illegal short term buffer|top block unavailable)/i;

/**
 * Точная проверка: декодирует окно источника и возвращает число реально
 * полученных кадров, ошибки декодера и непрочитанные участки («дыры»).
 */
async function probeDecode(media, start, duration, { ffprobePath, onSpawn } = {}) {
  const streamSel = Number.isInteger(media.videoIndex) ? String(media.videoIndex) : 'v:0';
  const s = Math.max(0, Number(start) || 0);
  const d = Math.max(0.05, Number(duration) || 0);
  const fps = media.fps > 0 ? media.fps : 30;
  const frameDur = 1 / fps;
  const fmt = await runProcess(ffprobePath, ['-v', 'error', '-show_entries', 'format=start_time', '-of', 'csv=p=0', media.file], { onSpawn, timeoutMs: 60000 });
  const baseTime = num(String(fmt.stdout || '').trim()) || 0;
  const times = [];
  const result = await runProcess(ffprobePath, [
    '-v', 'error', '-select_streams', streamSel,
    '-read_intervals', `${(s + baseTime).toFixed(3)}%${(s + d + baseTime + frameDur).toFixed(3)}`,
    '-show_entries', 'frame=best_effort_timestamp_time,pts_time', '-of', 'compact=p=0', media.file
  ], {
    onSpawn,
    onLine: (line) => {
      const f = parseCompact(line);
      const t = num(f.best_effort_timestamp_time != null && f.best_effort_timestamp_time !== 'N/A'
        ? f.best_effort_timestamp_time
        : f.pts_time);
      if (t != null) times.push(t - baseTime);
    }
  });
  const errorLines = String(result.stderr || '').split(/\r?\n/).filter((line) => DECODE_ERROR_RE.test(line));
  const inWindow = times.filter((t) => t >= s - frameDur / 2 && t < s + d).sort((a, b) => a - b);
  const holes = [];
  let cursor = s;
  inWindow.forEach((t) => {
    if (t - cursor > frameDur * 1.5) holes.push({ start: cursor, end: t });
    cursor = Math.max(cursor, t + frameDur);
  });
  if (s + d - cursor > frameDur * 1.5) holes.push({ start: cursor, end: s + d });
  return {
    start: s,
    duration: d,
    expectedFrames: Math.round(d * fps),
    frames: inWindow.length,
    firstFrame: inWindow.length ? inWindow[0] : null,
    errors: errorLines.length,
    errorLines: errorLines.slice(0, 8),
    holes,
    lostSeconds: holes.reduce((sum, h) => sum + (h.end - h.start), 0),
    exitCode: result.code
  };
}

/**
 * Разбор stderr упавшего рендера: ошибка декодера входа, отказ GPU или прочее.
 * Индекс входа — из префиксов ffmpeg 6/7: "[vist#3:0/h264 @ …]" и
 * "Error while decoding stream #3:0".
 */
function classifyFfmpegFailure(stderrText, extra = {}) {
  const text = String(stderrText || '');
  const lines = text.split(/\r?\n/);
  const decodeLines = lines.filter((line) => DECODE_ERROR_RE.test(line));
  const gpuLines = lines.filter((line) =>
    /(nvenc|OpenEncodeSession|No capable devices|NV_ENC|nvcuda|CUDA_ERROR|cuInit|\bcuda\b|_amf\b|\bAMF\b|_qsv\b|\bMFX\b|videotoolbox|hwaccel|hwupload|Device creation failed|Failed to (?:create|initialise|initialize) (?:the )?(?:device|encoder|session))/i.test(line)
  );
  const indexes = new Set(extra.inputIndexes || []);
  decodeLines.forEach((line) => {
    const m = line.match(/\[vist#(\d+):\d+/) || line.match(/decoding stream #(\d+):\d+/i);
    if (m) indexes.add(Number(m[1]));
  });
  const fatalDecode = decodeLines.some((line) => /Decode error rate|Decoding error|Error while decoding stream/i.test(line)) ||
    (decodeLines.length > 0 && /Conversion failed|FFmpeg завис/i.test(text));
  let kind = 'other';
  if (gpuLines.length) kind = 'gpu';
  else if (fatalDecode || (decodeLines.length && indexes.size)) kind = 'decode';
  return {
    kind,
    inputIndexes: Array.from(indexes).sort((a, b) => a - b),
    lines: (kind === 'gpu' ? gpuLines : decodeLines).slice(0, 12),
    decodeLines: decodeLines.slice(0, 12)
  };
}

/** Окна чтения входа: явное readWindow (для лупов) или -ss/-t из опций ffmpeg. */
function inputWindows(input, media) {
  if (input.readWindow && input.readWindow.duration > 0) return [{ ...input.readWindow }];
  const opts = input.options || [];
  const duration = media && media.duration > 0 ? media.duration : 0;
  if (opts.includes('-stream_loop')) return [{ start: 0, duration }];
  const ssAt = opts.indexOf('-ss');
  const tAt = opts.indexOf('-t');
  const start = ssAt >= 0 ? Number(opts[ssAt + 1]) || 0 : 0;
  const len = tAt >= 0 ? Number(opts[tAt + 1]) : Math.max(0, duration - start);
  return [{ start, duration: Number.isFinite(len) ? len : Math.max(0, duration - start) }];
}

const repairJobs = new Map();

function defaultCacheDir() {
  return path.join(os.tmpdir(), 'shorts-inserter-repair');
}

/**
 * Нормализованная копия повреждённого источника: видео перекодируется в H.264
 * (CRF 14) с исходными размером и частотой кадров; звук копируется (или в AAC,
 * если его нельзя положить в MP4 как есть). Результат кэшируется по пути,
 * размеру и mtime исходника.
 */
function buildNormalizedCopy(media, opts = {}) {
  const key = `${versionKey(media.file)}|v${REPAIR_VERSION}`;
  if (repairJobs.has(key)) return repairJobs.get(key);
  const job = buildNormalizedCopyUncached(media, key, opts).catch((err) => {
    repairJobs.delete(key);
    throw err;
  });
  repairJobs.set(key, job);
  return job;
}

async function buildNormalizedCopyUncached(media, key, { ffmpegPath, cacheDir, onSpawn, log, frameRate }) {
  const dir = cacheDir || defaultCacheDir();
  fs.mkdirSync(dir, { recursive: true });
  const hash = crypto.createHash('sha1').update(key).digest('hex').slice(0, 16);
  const base = path.basename(media.file).replace(/\.[^.]+$/, '').replace(/[^\w\-. ()]+/g, '_').slice(0, 60);
  const out = path.join(dir, `${hash}-${base}.repaired.mp4`);
  const metaFile = `${out}.json`;
  if (fs.existsSync(out) && fs.existsSync(metaFile)) {
    try {
      const saved = JSON.parse(fs.readFileSync(metaFile, 'utf8'));
      if (saved.key === key && saved.verified) {
        return { file: out, fromCache: true, meta: saved, metaFile };
      }
    } catch (err) {
      /* испорченный meta — пересоздаём */
    }
  }
  const part = `${out}.part`;
  const vIdx = Number.isInteger(media.videoIndex) ? media.videoIndex : 'v:0';
  const args = [
    '-hide_banner', '-nostdin', '-y', '-v', 'error',
    // Режим восстановления включается только после диагностики (ensureInputHealthy).
    '-err_detect', 'ignore_err', '-i', media.file,
    '-map', `0:${vIdx}`
  ];
  if (media.hasAudio) args.push('-map', `0:${Number.isInteger(media.audioIndex) ? media.audioIndex : 'a:0'}`);
  args.push(
    '-max_error_rate', '1',
    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-pix_fmt', 'yuv420p',
    '-g', String(Math.max(1, Math.round((media.fps || 30) * 2)))
  );
  // После потери пакетов декодер может выдать кадр не по порядку (сломанная
  // B-пирамида). Фильтр fps раскладывает кадры строго по сетке n/fps:
  // опоздавшие отбрасывает, дыры закрывает соседним кадром — число кадров и их
  // время совпадают с исходником (-fps_mode cfr на таком входе сдвигал всё
  // после дыры на кадр).
  if (!media.vfr) args.push('-vf', `fps=${frameRate || media.fps || 30}`);
  args.push('-fps_mode', 'passthrough');
  if (media.hasAudio) {
    const codec = String(media.audioCodec || '').toLowerCase();
    if (/^(aac|mp3|ac3|eac3|alac)$/.test(codec)) args.push('-c:a', 'copy');
    else args.push('-c:a', 'aac', '-b:a', '256k');
  }
  args.push('-map_metadata', '-1', '-movflags', '+faststart', '-f', 'mp4', part);
  const started = Date.now();
  if (log) log('warn', `Создаю нормализованную копию повреждённого источника ${path.basename(media.file)} (однократно, результат кэшируется)…`);
  const run = await runProcess(ffmpegPath, args, { onSpawn });
  if (run.code !== 0 || !fs.existsSync(part)) {
    try { fs.unlinkSync(part); } catch (err) { /* нет файла */ }
    throw new SourceCorruptError(
      `Не удалось создать восстановленную копию ${path.basename(media.file)}: ` +
        `${String(run.stderr || '').trim().split(/\r?\n/).slice(-3).join(' | ') || `код ${run.code}`}`,
      { file: media.file }
    );
  }
  fs.renameSync(part, out);
  const meta = { key, source: media.file, verified: false, createdAt: new Date().toISOString(), seconds: (Date.now() - started) / 1000 };
  return { file: out, fromCache: false, meta, metaFile };
}

/**
 * Стоят ли кадры копии на своих местах после повреждённого участка: PSNR по 10
 * кадрам. Сдвиг даже на один кадр на движущемся видео даёт ~20–29 дБ.
 */
async function alignmentPsnr(original, copy, at, { ffmpegPath, onSpawn } = {}) {
  const vo = Number.isInteger(original.videoIndex) ? original.videoIndex : 'v:0';
  const vc = Number.isInteger(copy.videoIndex) ? copy.videoIndex : 'v:0';
  const t = Math.max(0, at).toFixed(3);
  const run = await runProcess(ffmpegPath, [
    '-hide_banner', '-nostdin', '-ss', t, '-i', original.file, '-ss', t, '-i', copy.file,
    '-filter_complex', `[0:${vo}]setpts=PTS-STARTPTS[a];[1:${vc}]setpts=PTS-STARTPTS[b];[a][b]psnr`,
    '-frames:v', '10', '-f', 'null', '-'
  ], { onSpawn, timeoutMs: 120000 });
  const m = String(run.stderr || '').match(/PSNR .*average:(inf|[\d.]+)/);
  if (!m) return null;
  return m[1] === 'inf' ? Infinity : Number(m[1]);
}

function markRepairVerified(repair) {
  if (!repair || !repair.metaFile || repair.fromCache) return;
  try {
    fs.writeFileSync(repair.metaFile, JSON.stringify({ ...repair.meta, verified: true }, null, 2));
  } catch (err) {
    /* кэш не критичен */
  }
}

/**
 * Проверяет источник для его окон чтения и решает, что с ним делать.
 *
 * @returns {Promise<{media, status: 'ok'|'warn'|'repaired', notes: string[]}>}
 *   media — исходник или проверенная нормализованная копия (те же duration/fps/размер).
 * @throws {SourceCorruptError} если нужный участок восстановить нельзя.
 */
async function ensureInputHealthy(media, windows, ctx) {
  const { ffprobePath, ffmpegPath, probeMedia, log, onSpawn, cacheDir, afterFailure = false } = ctx;
  const name = path.basename(media.file);
  const notes = [];
  if (!fs.existsSync(media.file)) throw new SourceCorruptError(`Источник недоступен: ${media.file}`, { file: media.file });
  if (!(media.width > 0 && media.height > 0)) {
    throw new SourceCorruptError(`В источнике ${name} нет видеопотока`, { file: media.file });
  }
  const tolerance = Math.max(0.1, 2 / (media.fps || 30));
  windows.forEach((w) => {
    if (!(w.duration > 0) || w.start < -1e-3 || (media.duration > 0 && w.start >= media.duration + 1e-3)) {
      throw new SourceCorruptError(
        `Некорректный участок чтения ${clock(w.start)} +${(w.duration || 0).toFixed(3)} с для ${name} (длительность ${clock(media.duration)})`,
        { file: media.file, window: w }
      );
    }
    if (media.duration > 0 && w.start + w.duration > media.duration + tolerance + 0.1) {
      notes.push(`участок ${clock(w.start)}–${clock(w.start + w.duration)} выходит за конец файла ${clock(media.duration)}`);
    }
  });

  let scan = null;
  try {
    scan = await scanPackets(media, { ffprobePath, onSpawn });
  } catch (err) {
    if (err.sourceCorrupt && afterFailure) throw err;
    // До рендера нечитаемый контейнер не обрываем: рендер упадёт с полным stderr
    // ffmpeg, и разбор пойдёт по пути «ошибка в рендере».
    notes.push(`структурная проверка не удалась: ${err.message}`);
    return { media, status: 'warn', notes, scan: null };
  }
  if (scan && scan.videoEnd > 0) {
    windows.forEach((w) => {
      if (w.start >= scan.videoEnd - 1e-3) {
        throw new SourceCorruptError(
          `${name}: участок ${clock(w.start)} начинается после конца видеодорожки ${clock(scan.videoEnd)}`,
          { file: media.file, window: w }
        );
      }
    });
  }

  const suspicious = [];
  windows.forEach((w) => {
    const hits = scan ? rangesInWindow(scan, w.start, w.duration) : [];
    const ts = scan ? scan.tsIssues.filter((i) => i.t >= w.start && i.t < w.start + w.duration) : [];
    if (hits.length || ts.length || afterFailure) suspicious.push({ window: w, hits, ts });
  });
  // Быстрый путь: в нужных окнах нет ни битых пакетов, ни проблем с метками времени.
  if (!suspicious.length) return { media, status: notes.length ? 'warn' : 'ok', notes, scan };

  let needCopy = false;
  const fatal = [];
  for (const item of suspicious) {
    const w = item.window;
    const probe = await probeDecode(media, w.start, w.duration, { ffprobePath, onSpawn });
    item.probe = probe;
    const hitText = item.hits.length
      ? `повреждённые пакеты ${rangeText(item.hits)} (${item.hits.reduce((n, r) => n + r.packets, 0)} шт., ${item.hits[0].reason}); ` +
        `артефакты возможны до ключевого кадра ${clock(item.hits[item.hits.length - 1].affectedEnd)}`
      : '';
    const tsText = item.ts.length ? `метки времени: ${item.ts.slice(0, 3).map((i) => `${clock(i.t)} ${i.reason}`).join('; ')}` : '';
    const msg = `${name}, окно ${clock(w.start)}–${clock(w.start + w.duration)}: декодировано ${probe.frames} из ~${probe.expectedFrames} кадров, ` +
      `ошибок декодера ${probe.errors}` +
      `${probe.holes.length ? `, нечитаемо ${rangeText(probe.holes)} (${probe.lostSeconds.toFixed(2)} с)` : ''}` +
      `${hitText ? `; ${hitText}` : ''}${tsText ? `; ${tsText}` : ''}`;
    if (!probe.errors && !probe.holes.length && !item.hits.length) {
      if (item.ts.length) notes.push(msg);
      continue;
    }
    if (probe.frames === 0 || probe.lostSeconds > MAX_CONCEALED_SECONDS) {
      fatal.push({ msg, window: w, probe, hits: item.hits });
      continue;
    }
    // Дыра внутри окна не сдвигает монтаж (метки времени сохраняются, CFR-выход
    // повторит соседний кадр) — достаточно предупреждения. Копия нужна, если окно
    // начинается с нечитаемого участка (setpts=PTS-STARTPTS сдвинул бы картинку),
    // если доля ошибок угрожает лимиту ffmpeg или рендер с этим входом уже упал.
    const headHole = probe.holes.some((h) => h.start <= w.start + 1e-3);
    const errorShare = probe.errors / Math.max(1, probe.frames + probe.errors);
    const missingShare = probe.expectedFrames ? 1 - probe.frames / probe.expectedFrames : 1;
    if (headHole || errorShare > RISKY_ERROR_SHARE || missingShare > 0.5 || afterFailure) needCopy = true;
    notes.push(msg);
  }

  if (fatal.length) {
    const first = fatal[0];
    throw new SourceCorruptError(
      `Источник повреждён: ${first.msg}. Восстановить этот участок без «замороженного» видео нельзя ` +
        `(допустимо до ${MAX_CONCEALED_SECONDS} с) — замените или перекачайте файл.`,
      { file: media.file, windows: fatal.map((f) => f.window), probes: fatal.map((f) => f.probe), ranges: fatal.flatMap((f) => f.hits) }
    );
  }
  if (!needCopy) return { media, status: 'warn', notes, scan };

  const repair = await buildNormalizedCopy(media, {
    ffmpegPath, cacheDir, onSpawn, log, frameRate: scan && scan.rFrameRate
  });
  const repairedInfo = await probeMedia(repair.file);
  const repaired = {
    ...media,
    file: repair.file,
    originalFile: media.file,
    repaired: true,
    rotation: 0,
    videoIndex: repairedInfo.videoIndex,
    audioIndex: repairedInfo.audioIndex,
    hasAudio: repairedInfo.hasAudio && media.hasAudio,
    videoCodec: repairedInfo.videoCodec,
    audioCodec: repairedInfo.audioCodec,
    pixelFormat: repairedInfo.pixelFormat
  };
  // Проверка копии: размер, длительность, число кадров, окна без ошибок и дыр,
  // кадры после повреждения на своих местах.
  const problems = [];
  if (Math.abs(repairedInfo.width - media.width) > 2 || Math.abs(repairedInfo.height - media.height) > 2) {
    problems.push(`размер ${repairedInfo.width}x${repairedInfo.height} вместо ${media.width}x${media.height}`);
  }
  const refDuration = scan && scan.videoEnd ? Math.min(media.duration, scan.videoEnd) : media.duration;
  const gotDuration = repairedInfo.videoDuration || repairedInfo.duration;
  if (refDuration > 0 && Math.abs(gotDuration - refDuration) > Math.max(0.25, 3 / (media.fps || 30))) {
    problems.push(`длительность видео ${gotDuration.toFixed(3)} с вместо ${refDuration.toFixed(3)} с`);
  }
  const repairedScan = await scanPackets(repaired, { ffprobePath, onSpawn });
  if (repairedScan.badPackets) problems.push(`в копии ${repairedScan.badPackets} повреждённых пакетов`);
  if (!media.vfr && scan && scan.packets && Math.abs(repairedScan.packets - scan.packets) > 1) {
    problems.push(`в копии ${repairedScan.packets} кадров вместо ${scan.packets}`);
  }
  for (const item of suspicious) {
    const w = item.window;
    const check = await probeDecode(repaired, w.start, w.duration, { ffprobePath, onSpawn });
    if (check.errors || check.holes.length || check.frames < check.expectedFrames - 1) {
      problems.push(`окно ${clock(w.start)}: ${check.frames}/${check.expectedFrames} кадров, ошибок ${check.errors}`);
    }
  }
  const checkpoints = suspicious.flatMap((item) => item.hits.map((r) => r.affectedEnd + 0.5))
    .filter((t) => t < refDuration - 0.5).slice(0, 4);
  for (const at of checkpoints) {
    const psnr = await alignmentPsnr(media, repaired, at, { ffmpegPath, onSpawn });
    if (psnr != null && psnr < MIN_ALIGNMENT_PSNR) problems.push(`кадры после ${clock(at)} сдвинуты (PSNR ${psnr.toFixed(1)} дБ)`);
  }
  if (problems.length) {
    throw new SourceCorruptError(
      `Нормализованная копия ${name} не прошла проверку (${problems.join('; ')}). Файл повреждён: ${notes.join(' | ')}`,
      { file: media.file }
    );
  }
  markRepairVerified(repair);
  notes.push(`используется проверенная нормализованная копия ${path.basename(repair.file)}${repair.fromCache ? ' (из кэша)' : ''}`);
  return { media: repaired, status: 'repaired', notes, scan };
}

module.exports = {
  SourceDecodeError,
  SourceCorruptError,
  MAX_CONCEALED_SECONDS,
  DECODE_ERROR_RE,
  scanPackets,
  rangesInWindow,
  probeDecode,
  classifyFfmpegFailure,
  inputWindows,
  ensureInputHealthy,
  buildNormalizedCopy,
  alignmentPsnr,
  checkPacketNal,
  parseHexDump,
  nalLengthSize,
  clock
};
