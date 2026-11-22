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
  MIN_SHORT_SECONDS,
  MAX_SINCE_VIDEOS,
  normalizeChannelUrl,
  parseChannelList,
  classifyFresh,
  normalizeVideoCount,
  normalizeSinceDate
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
  check(normalizeChannelUrl('@SomeChannel') === 'https://www.youtube.com/@SomeChannel/shorts', '@имя открывает вкладку Shorts');
  check(
    normalizeChannelUrl('https://m.youtube.com/@SomeChannel/shorts') === 'https://www.youtube.com/@SomeChannel/shorts',
    'мобильная ссылка на Shorts сохраняет вкладку'
  );
  check(normalizeChannelUrl('https://www.youtube.com/channel/UCabcdefghijk/videos') === 'https://www.youtube.com/channel/UCabcdefghijk/shorts', 'channel/UC и вкладка видео открывают Shorts');
  check(
    normalizeChannelUrl('https://www.youtube.com/@SomeChannel/featured') === 'https://www.youtube.com/@SomeChannel/shorts',
    'вкладка featured открывает Shorts этого канала'
  );
  check(normalizeChannelUrl('https://www.youtube.com/watch?v=AAAAAAAAAAA') === null, 'ссылка на ролик не считается каналом');
  check(normalizeChannelUrl('https://youtu.be/AAAAAAAAAAA') === null, 'youtu.be не считается каналом');
  const parsed = parseChannelList(`
    @One
    https://www.youtube.com/@One/shorts
    https://www.youtube.com/@Two/shorts
    https://www.youtube.com/watch?v=AAAAAAAAAAA
    мусор
  `);
  check(parsed.length === 2, 'дубликаты и не-каналы отбрасываются', parsed.join(' | '));
  check(normalizeVideoCount(0) === 1 && normalizeVideoCount(99) === 50 && normalizeVideoCount('3') === 3, 'число роликов в диапазоне 1…50');
  check(MAX_SHORT_SECONDS === 150, 'порог короткого ролика — 2,5 минуты');
  check(MIN_SHORT_SECONDS === 35, 'короче 35 секунд Shorts не берётся');
  check(normalizeSinceDate('2026-11-10') === '20261110', 'дата из календаря становится YYYYMMDD');
  check(normalizeSinceDate('10.11.2026') === '20261110', 'дата ДД.ММ.ГГГГ принимается');
  check(normalizeSinceDate('2026-02-31') === null, 'несуществующий день отвергается');

  const fresh = classifyFresh(
    [
      { id: 'NEWSHORT111', url: 'https://www.youtube.com/watch?v=NEWSHORT111', duration: 59 },
      { id: 'SEENSHORT11', url: 'https://www.youtube.com/watch?v=SEENSHORT11', duration: 40 },
      { id: 'TOOLONG1111', url: 'https://www.youtube.com/watch?v=TOOLONG1111', duration: 180 },
      { id: 'EXACTLY150S', url: 'https://www.youtube.com/watch?v=EXACTLY150S', duration: 150 },
      { id: 'OVER150SEC1', url: 'https://www.youtube.com/watch?v=OVER150SEC1', duration: 150.1 },
      { id: 'JUSTUNDER12', url: 'https://www.youtube.com/watch?v=JUSTUNDER12', duration: 119.9 },
      { id: 'EXACTLY35S', url: 'https://www.youtube.com/watch?v=EXACTLY35S', duration: 35 },
      { id: 'TOOSHORT34', url: 'https://www.youtube.com/watch?v=TOOSHORT34', duration: 34 }
    ],
    new Set(['SEENSHORT11'])
  );
  check(!fresh.ignored, 'есть новое видео — канал не игнорируется');
  check(
    fresh.links.length === 4 && fresh.links[0].includes('NEWSHORT111') && fresh.links[1].includes('EXACTLY150S') && fresh.links[2].includes('JUSTUNDER12') && fresh.links[3].includes('EXACTLY35S'),
    'в ссылки попадают ролики от 35 до 150 секунд',
    fresh.links.join(',')
  );
  check(fresh.tooShort === 1 && fresh.markSeen.includes('TOOSHORT34'), 'короче 35 секунд не попадает в файл и запоминается', String(fresh.tooShort));
  check(fresh.tooLong === 2, 'дольше 2,5 минут не попадает в файл', String(fresh.tooLong));
  check(fresh.markSeen.includes('TOOLONG1111') && fresh.markSeen.includes('OVER150SEC1') && fresh.markSeen.includes('EXACTLY150S'), 'длинные ролики запоминаются, чтобы не прийти снова');
  check(classifyFresh([{ id: 'SEENSHORT11', duration: 30, url: 'u' }], ['SEENSHORT11']).ignored, 'если новых видео нет, канал игнорируется');

  console.log('\n2) Один последний ролик короче 2 минут пишется в Links.txt…');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(ROOT, { recursive: true });
  fs.writeFileSync(path.join(ROOT, LINKS_FILE), 'https://www.youtube.com/watch?v=OLDVIDEOxxx\n', 'utf8');
  const catalogs = {
    'https://www.youtube.com/@Alpha/shorts': playlist('UCalpha', [
      { id: 'ALPHASHORT1', duration: 48, title: 'new short' },
      { id: 'ALPHAOLD111', duration: 20, title: 'older' }
    ]),
    'https://www.youtube.com/@Beta/shorts': playlist('UCbeta', [
      { id: 'BETALONG111', duration: 300, title: 'long' }
    ]),
    'https://www.youtube.com/@Gamma/shorts': playlist('UCgamma', [
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
    concurrency: 1,
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
    const second = await again.run('https://www.youtube.com/@Alpha/shorts\nhttps://www.youtube.com/@Beta/shorts');
    check(second.ignored === 2 && second.links === 0, 'оба канала проигнорированы', JSON.stringify(second));
    const linesAfter = fs.readFileSync(path.join(ROOT, LINKS_FILE), 'utf8').split(/\r?\n/).filter(Boolean);
    check(linesAfter.length === 3, 'Links.txt не получил повторную ссылку', linesAfter.join(' | '));

    console.log('\n4) Новое короткое видео канала попадает в файл, старое — нет…');
    catalogs['https://www.youtube.com/@Alpha/shorts'] = playlist('UCalpha', [
      { id: 'ALPHANEW222', duration: 36, title: 'newer' },
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
      concurrency: 1,
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

    console.log('\n6) С выбранной даты берутся все ролики до 2,5 минут…');
    const sinceDir = path.join(ROOT, 'since');
    fs.mkdirSync(sinceDir, { recursive: true });
    const day = (ymd) => Math.floor(Date.UTC(Number(ymd.slice(0, 4)), Number(ymd.slice(4, 6)) - 1, Number(ymd.slice(6, 8)), 12) / 1000);
    let playlistEnd = null;
    const flatUrls = [];
    const sinceCollector = new ChannelCollector({
      outputDir: sinceDir,
      since: '2026-11-10',
      count: 1,
      runner: async (args) => {
        if (args.includes('--flat-playlist')) {
          const endAt = args.indexOf('--playlist-end');
          playlistEnd = endAt >= 0 ? args[endAt + 1] : null;
          flatUrls.push(args[args.length - 1]);
        }
        return playlist('UCsince', [
          { id: 'NODATESHORT', duration: 40, title: 'short without date' },
          { id: 'PINNEDOLD01', duration: 40, timestamp: day('20200101'), title: 'pinned old' },
          { id: 'NOV12SHORT1', duration: 60, timestamp: day('20261112'), title: 'after pinned' },
          { id: 'NOV20SHORT1', duration: 140, timestamp: day('20261120'), title: '20 nov' },
          { id: 'NOV10EXACT1', duration: 150, timestamp: day('20261110'), title: '10 nov exact' },
          { id: 'NOV10TOOLNG', duration: 151, timestamp: day('20261110'), title: '10 nov long' },
          { id: 'NOV09SHORT1', duration: 30, timestamp: day('20261109'), title: 'before date' },
          { id: 'NOV01SHORT1', duration: 20, timestamp: day('20261101'), title: 'even older' }
        ]);
      }
    });
    const sinceSummary = await sinceCollector.run('@Since');
    check(String(flatUrls[0] || '').endsWith('/@Since/shorts'), 'запрос идёт на вкладку Shorts', flatUrls.join(' | '));
    check(flatUrls.some((url) => url.includes('list=UUSHsince')), 'после короткой вкладки открывается полный список Shorts', flatUrls.join(' | '));
    check(playlistEnd === String(MAX_SINCE_VIDEOS), 'с даты запрашивается окно роликов, а не одно последнее', playlistEnd);
    check(sinceSummary.links === 4 && sinceSummary.tooLong === 1, 'с 10 ноября записаны Shorts до 150 с, более длинный нет', JSON.stringify(sinceSummary));
    const sinceLines = fs.readFileSync(path.join(sinceDir, LINKS_FILE), 'utf8');
    check(sinceLines.includes('NODATESHORT') && sinceLines.includes('NOV20SHORT1') && sinceLines.includes('NOV10EXACT1') && sinceLines.includes('NOV12SHORT1'), 'Shorts без даты, с даты и в сам день попали в Links.txt');
    check(!sinceLines.includes('NOV09SHORT1') && !sinceLines.includes('NOV01SHORT1') && !sinceLines.includes('PINNEDOLD01'), 'ролики раньше выбранной даты не записаны');
    const sinceHistory = JSON.parse(fs.readFileSync(path.join(sinceDir, HISTORY_FILE), 'utf8'));
    check(sinceHistory.channels.UCsince.seen.includes('NOV10TOOLNG'), 'длинный ролик с этой даты запомнен');
    check(!sinceHistory.channels.UCsince.seen.includes('NOV09SHORT1'), 'ролик раньше даты не помечается просмотренным');

    console.log('\n7) Несколько каналов проверяются одновременно…');
    const parallelDir = path.join(ROOT, 'parallel');
    fs.mkdirSync(parallelDir, { recursive: true });
    let active = 0;
    let maxActive = 0;
    const parallelCollector = new ChannelCollector({
      outputDir: parallelDir,
      since: '2026-11-10',
      concurrency: 2,
      runner: async (args) => {
        if (!args.includes('--flat-playlist')) return playlist('UCprobe', []);
        active += 1;
        maxActive = Math.max(maxActive, active);
        await new Promise((resolve) => setTimeout(resolve, 40));
        active -= 1;
        const left = String(args[args.length - 1]).includes('@Left');
        return playlist(left ? 'UCleft' : 'UCright', [
          { id: left ? 'LEFTSHORT11' : 'RIGHTSHORT1', duration: left ? 20 : 50, timestamp: day('20261120'), title: left ? 'too short' : 'ok' }
        ]);
      }
    });
    const parallelSummary = await parallelCollector.run('@Left\n@Right');
    check(maxActive >= 2, 'два канала запрашиваются одновременно', `max=${maxActive}`);
    check(parallelSummary.links === 1 && parallelSummary.tooShort === 1, 'поток записал Shorts от 35 с и пропустил более короткий', JSON.stringify(parallelSummary));
    const parallelLines = fs.readFileSync(path.join(parallelDir, LINKS_FILE), 'utf8');
    check(parallelLines.includes('RIGHTSHORT1') && !parallelLines.includes('LEFTSHORT11'), 'короче 35 секунд не записан, подходящий записан');

    console.log('\n8) Сотни роликов без длительности не открываются по одному…');
    const thickDir = path.join(ROOT, 'thick');
    fs.mkdirSync(thickDir, { recursive: true });
    let probes = 0;
    const bare = Array.from({ length: 12 }, (_, index) => ({ id: `BARE${String(index).padStart(7, '0')}`, title: 'bare' }));
    const rich = bare.map((entry, index) => ({ ...entry, duration: 40 + (index % 20) }));
    const thick = new ChannelCollector({
      outputDir: thickDir,
      since: '2026-11-10',
      concurrency: 1,
      runner: async (args) => {
        if (!args.includes('--flat-playlist')) {
          probes += 1;
          return { stdout: JSON.stringify({ duration: 40 }), stderr: '' };
        }
        const url = String(args[args.length - 1]);
        if (url.includes('list=UUSH')) return playlist('UCthick01', rich);
        return playlist('UCthick01', bare);
      }
    });
    const thickSummary = await thick.run('@Thick');
    check(probes === 0, 'длительность взята из полного списка, ролики по одному не открывались', `probes=${probes}`);
    check(thickSummary.links === 12, 'все Shorts с длительностью из полного списка записаны', JSON.stringify(thickSummary));

    const stuckDir = path.join(ROOT, 'stuck');
    fs.mkdirSync(stuckDir, { recursive: true });
    let stuckProbes = 0;
    const stuck = new ChannelCollector({
      outputDir: stuckDir,
      since: '2026-11-10',
      concurrency: 1,
      runner: async (args) => {
        if (!args.includes('--flat-playlist')) {
          stuckProbes += 1;
          return { stdout: '{}', stderr: '' };
        }
        if (String(args[args.length - 1]).includes('list=UUSH')) throw new Error('playlist unavailable');
        return playlist('UCstuck01', bare);
      }
    });
    const stuckSummary = await stuck.run('@Stuck');
    check(
      stuckProbes === 0 && stuckSummary.links === 0 && stuckSummary.errors === 0 && stuckSummary.unknown === 12,
      'если длительности нет у многих роликов, сбор заканчивается без проверки каждого',
      JSON.stringify(stuckSummary)
    );

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
