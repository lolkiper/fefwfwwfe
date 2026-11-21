'use strict';

/**
 * Сбор коротких ссылок с каналов без запросов к YouTube.
 *
 *   node scripts/channels-smoke-test.js
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  ChannelCollector,
  LINKS_FILE,
  HISTORY_FILE,
  MAX_SHORT_SECONDS,
  normalizeChannelUrl,
  parseChannelList,
  classifyFresh,
  normalizeVideoCount
} = require('../channels');

const ROOT = path.join(os.tmpdir(), `shorts-channels-smoke-${process.pid}`);
let failures = 0;

function check(condition, description, details = '') {
  if (condition) console.log(`  OK   ${description}`);
  else {
    failures += 1;
    console.log(`  FAIL ${description}${details ? ` — ${details}` : ''}`);
  }
}

function playlist(channelId, entries) {
  return {
    stdout: JSON.stringify({
      channel_id: channelId,
      entries
    }),
    stderr: ''
  };
}

function main() {
  console.log('1) Разбор ссылок на каналы…');
  check(normalizeChannelUrl('@SomeChannel') === 'https://www.youtube.com/@SomeChannel/videos', '@имя становится вкладкой видео');
  check(
    normalizeChannelUrl('https://m.youtube.com/@SomeChannel/shorts') === 'https://www.youtube.com/@SomeChannel/shorts',
    'мобильная ссылка на Shorts сохраняет вкладку'
  );
  check(normalizeChannelUrl('https://www.youtube.com/channel/UCabcdefghijk/videos').includes('/channel/UCabcdefghijk/videos'), 'channel/UC принимается');
  check(
    normalizeChannelUrl('https://www.youtube.com/@SomeChannel/featured') === 'https://www.youtube.com/@SomeChannel/videos',
    'вкладка featured считается каналом и открывает видео'
  );
  check(normalizeChannelUrl('https://www.youtube.com/watch?v=AAAAAAAAAAA') === null, 'ссылка на ролик не считается каналом');
  check(normalizeChannelUrl('https://youtu.be/AAAAAAAAAAA') === null, 'youtu.be не считается каналом');
  const parsed = parseChannelList(`
    @One
    https://www.youtube.com/@One/videos
    https://www.youtube.com/@Two/shorts
    https://www.youtube.com/watch?v=AAAAAAAAAAA
    мусор
  `);
  check(parsed.length === 2, 'дубликаты и не-каналы отбрасываются', parsed.join(' | '));
  check(normalizeVideoCount(0) === 1 && normalizeVideoCount(99) === 50 && normalizeVideoCount('3') === 3, 'число роликов в диапазоне 1…50');
  check(MAX_SHORT_SECONDS === 120, 'порог короткого ролика — 2 минуты');

  const fresh = classifyFresh(
    [
      { id: 'NEWSHORT111', url: 'https://www.youtube.com/watch?v=NEWSHORT111', duration: 59 },
      { id: 'SEENSHORT11', url: 'https://www.youtube.com/watch?v=SEENSHORT11', duration: 40 },
      { id: 'TOOLONG1111', url: 'https://www.youtube.com/watch?v=TOOLONG1111', duration: 180 },
      { id: 'EXACTLY1200', url: 'https://www.youtube.com/watch?v=EXACTLY1200', duration: 120 },
      { id: 'JUSTUNDER12', url: 'https://www.youtube.com/watch?v=JUSTUNDER12', duration: 119.9 }
    ],
    new Set(['SEENSHORT11']),
    120
  );
  check(!fresh.ignored, 'есть новое видео — канал не игнорируется');
  check(fresh.links.length === 2 && fresh.links[0].includes('NEWSHORT111') && fresh.links[1].includes('JUSTUNDER12'), 'в ссылки попадают только ролики короче 2 минут', fresh.links.join(','));
  check(fresh.tooLong === 2, 'ровно 2 минуты и длиннее не попадают в файл', String(fresh.tooLong));
  check(fresh.markSeen.includes('TOOLONG1111') && fresh.markSeen.includes('EXACTLY1200'), 'длинные ролики запоминаются, чтобы не прийти снова');
  check(classifyFresh([{ id: 'SEENSHORT11', duration: 30, url: 'u' }], ['SEENSHORT11']).ignored, 'если новых видео нет, канал игнорируется');

  console.log('\n2) Один последний ролик короче 2 минут пишется в Links.txt…');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(ROOT, { recursive: true });
  fs.writeFileSync(path.join(ROOT, LINKS_FILE), 'https://www.youtube.com/watch?v=OLDVIDEOxxx\n', 'utf8');
  const catalogs = {
    'https://www.youtube.com/@Alpha/videos': playlist('UCalpha', [
      { id: 'ALPHASHORT1', duration: 48, title: 'new short' },
      { id: 'ALPHAOLD111', duration: 20, title: 'older' }
    ]),
    'https://www.youtube.com/@Beta/videos': playlist('UCbeta', [
      { id: 'BETALONG111', duration: 300, title: 'long' }
    ]),
    'https://www.youtube.com/@Gamma/videos': playlist('UCgamma', [
      { id: 'GAMMANODUR1', duration: null, title: '' }
    ]),
    'https://www.youtube.com/watch?v=GAMMANODUR1': {
      stdout: JSON.stringify({ id: 'GAMMANODUR1', duration: 55, title: 'probed short' }),
      stderr: ''
    }
  };
  const runner = async (args) => {
    const url = args[args.length - 1];
    if (args.includes('--flat-playlist')) {
      if (!args.includes('--yes-playlist') || args.includes('--no-playlist')) {
        throw new Error('список канала запрошен не как плейлист');
      }
    } else if (String(url).includes('watch?v=')) {
      if (!args.includes('--no-playlist') || !args.includes('--skip-download')) {
        throw new Error('ролик без длительности должен проверяться отдельно');
      }
    }
    if (!catalogs[url]) throw new Error(`неизвестный канал ${url}`);
    return catalogs[url];
  };
  const collector = new ChannelCollector({
    outputDir: ROOT,
    count: 1,
    runner
  });
  return collector.run('@Alpha\nhttps://www.youtube.com/@Beta\n@Gamma').then(async (summary) => {
    check(summary.links === 2, 'записаны короткие ссылки, в том числе после проверки длительности', JSON.stringify(summary));
    check(summary.ignored === 0 && summary.tooLong === 1, 'длинный ролик не дал ссылку', JSON.stringify(summary));
    const lines = fs.readFileSync(path.join(ROOT, LINKS_FILE), 'utf8').split(/\r?\n/).filter(Boolean);
    check(lines[0].includes('OLDVIDEOxxx') && lines[1].includes('ALPHASHORT1') && lines[2].includes('GAMMANODUR1'), 'старые строки Links.txt остаются, новые ссылки дописываются после них', lines.join(' | '));
    check(!lines.some((line) => line.includes('BETALONG111')), 'ролик длиннее 2 минут не попал в файл');
    const history = JSON.parse(fs.readFileSync(path.join(ROOT, HISTORY_FILE), 'utf8'));
    check(history.channels.UCalpha.seen.includes('ALPHASHORT1'), 'короткий ролик записан в историю');
    check(history.channels.UCbeta.seen.includes('BETALONG111'), 'длинный ролик тоже в истории, чтобы не повторяться');
    check(history.channels.UCgamma.seen.includes('GAMMANODUR1'), 'ролик без длительности в плейлисте проверен и записан');

    console.log('\n3) Повтор того же канала без новых видео ничего не дописывает…');
    const again = new ChannelCollector({ outputDir: ROOT, count: 1, runner });
    const second = await again.run('https://www.youtube.com/@Alpha/videos\nhttps://www.youtube.com/@Beta/videos');
    check(second.ignored === 2 && second.links === 0, 'оба канала проигнорированы', JSON.stringify(second));
    const linesAfter = fs.readFileSync(path.join(ROOT, LINKS_FILE), 'utf8').split(/\r?\n/).filter(Boolean);
    check(linesAfter.length === 3, 'Links.txt не получил повторную ссылку', linesAfter.join(' | '));

    console.log('\n4) Новое короткое видео канала попадает в файл, старое — нет…');
    catalogs['https://www.youtube.com/@Alpha/videos'] = playlist('UCalpha', [
      { id: 'ALPHANEW222', duration: 33, title: 'newer' },
      { id: 'ALPHASHORT1', duration: 48, title: 'previous' }
    ]);
    const thirdCollector = new ChannelCollector({ outputDir: ROOT, count: 2, runner });
    const third = await thirdCollector.run('@Alpha');
    check(third.links === 1 && third.ignored === 0, 'добавилась только новая ссылка', JSON.stringify(third));
    const linesThird = fs.readFileSync(path.join(ROOT, LINKS_FILE), 'utf8').split(/\r?\n/).filter(Boolean);
    check(linesThird.filter((line) => line.includes('ALPHASHORT1')).length === 1, 'старая ссылка не продублирована');
    check(linesThird[linesThird.length - 1].includes('ALPHANEW222'), 'новая ссылка в конце файла');

    console.log('\n5) Стоп не обходит остальные каналы…');
    const stopDir = path.join(ROOT, 'stop');
    fs.mkdirSync(stopDir, { recursive: true });
    let calls = 0;
    let stopper = null;
    stopper = new ChannelCollector({
      outputDir: stopDir,
      count: 1,
      runner: async (args) => {
        calls += 1;
        const url = args[args.length - 1];
        if (url.includes('@StopMe')) stopper.stop();
        return playlist(url.includes('@StopMe') ? 'UCstop' : 'UCnext', [
          { id: url.includes('@StopMe') ? 'STOPVID1111' : 'NEXTVID1111', duration: 20 }
        ]);
      }
    });
    const stopped = await stopper.run('@StopMe\n@NextOne');
    check(stopped.cancelled === true, 'сбор помечен как остановленный');
    check(calls === 1, 'после Стоп следующий канал не запрашивается', `calls=${calls}`);
    const stopLinks = fs.readFileSync(path.join(stopDir, LINKS_FILE), 'utf8');
    check(!stopLinks.includes('NEXTVID1111'), 'ссылка непроверенного канала не записана');
    check(!stopLinks.includes('STOPVID1111'), 'канал, на котором нажали Стоп, не дописывает ссылку');

    fs.rmSync(ROOT, { recursive: true, force: true });
    console.log(`\nИтог: ${failures ? `${failures} проверок провалено` : 'все проверки каналов пройдены'}`);
    process.exit(failures ? 1 : 0);
  });
}

main().catch((err) => {
  console.error('\nТест каналов упал:', err);
  fs.rmSync(ROOT, { recursive: true, force: true });
  process.exit(1);
});
