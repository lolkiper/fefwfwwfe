'use strict';

/**
 * Диагностика одного источника тем же кодом, что и перед рендером.
 *
 *   node scripts/diagnose-source.js "<файл.mp4>" [начало_сек] [длительность_сек]
 *
 * Печатает: структуру пакетов (битые NAL, метки времени), участки повреждения и,
 * если задано окно, результат реального декодирования этого окна.
 */

const path = require('path');
const { probeMedia, ffprobePath } = require('../processor');
const health = require('../source-health');

async function main() {
  const [file, startArg, durArg] = process.argv.slice(2);
  if (!file) {
    console.log('Использование: node scripts/diagnose-source.js <файл> [начало_сек] [длительность_сек]');
    process.exit(2);
  }
  const media = await probeMedia(file);
  console.log(`${path.basename(file)}: ${media.width}x${media.height}, ${media.fps} fps, ${health.clock(media.duration)}, ` +
    `видео ${media.videoCodec}, звук ${media.audioCodec || 'нет'}`);
  const scan = await health.scanPackets(media, { ffprobePath });
  console.log(`Пакетов видео: ${scan.packets}; конец видеодорожки ${health.clock(scan.videoEnd)}; ` +
    `проверка длин NAL: ${scan.structureChecked ? `да (префикс ${scan.nalLengthSize} байта)` : 'нет (не AVCC/HVCC)'}`);
  console.log(`Битых пакетов: ${scan.badPackets}`);
  scan.badRanges.forEach((r) => console.log(
    `  ${health.clock(r.start)}–${health.clock(r.end)}: ${r.packets} пакетов (${r.reason}); ` +
      `артефакты до ключевого кадра ${health.clock(r.affectedEnd)}`
  ));
  scan.tsIssues.slice(0, 10).forEach((i) => console.log(`  метки времени: ${health.clock(i.t)} ${i.reason}`));
  scan.demuxErrors.slice(0, 5).forEach((line) => console.log(`  ffprobe: ${line}`));
  if (startArg != null) {
    const start = Number(startArg);
    const duration = Number(durArg) > 0 ? Number(durArg) : 60;
    const probe = await health.probeDecode(media, start, duration, { ffprobePath });
    console.log(`Окно ${health.clock(start)} +${duration} с: декодировано ${probe.frames} из ~${probe.expectedFrames} кадров, ` +
      `ошибок декодера ${probe.errors}`);
    probe.holes.forEach((h) => console.log(`  нечитаемо ${health.clock(h.start)}–${health.clock(h.end)}`));
    probe.errorLines.slice(0, 5).forEach((line) => console.log(`  ${line}`));
  }
  process.exit(scan.badPackets ? 1 : 0);
}

main().catch((err) => {
  console.error(err.message);
  process.exit(3);
});
