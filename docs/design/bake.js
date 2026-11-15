// docs/design/*.svg 를 README용 그림(docs/images/readme/)으로 굽는다.
// GitHub는 <img> 속 SVG에서 웹 글꼴을 쓰지 못하므로, fonts.txt의 글꼴을 불러온 브라우저에서 2배 크기로 찍는다.
//
//   npm i --no-save puppeteer-core
//   CHROME="<Chrome 또는 Edge 실행 파일 경로>" node docs/design/bake.js
//
// hero는 실제 화면 두 장(../images/v2/)을 합성한 사진 같은 그림이라 WebP로, 나머지는 PNG로 저장한다.
'use strict';
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const puppeteer = require('puppeteer-core');

const DESIGN = __dirname;
const OUT = path.join(DESIGN, '..', 'images', 'readme');
const WEBP = new Set(['hero']);
const CHROME = process.env.CHROME;
if (!CHROME) {
  console.error('CHROME 환경 변수에 Chrome 또는 Edge 실행 파일 경로를 넣어 주세요.');
  process.exit(1);
}

const families = fs
  .readFileSync(path.join(DESIGN, 'fonts.txt'), 'utf8')
  .split(/\r?\n/)
  .map((s) => s.trim())
  .filter(Boolean);
const fontLink = `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?${families.map((f) => 'family=' + f).join('&')}&display=block">`;

function size(svg) {
  const tag = /<svg\b[^>]*>/i.exec(svg)[0];
  return [+/\swidth="([\d.]+)"/.exec(tag)[1], +/\sheight="([\d.]+)"/.exec(tag)[1]];
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--allow-file-access-from-files'] });
  try {
    for (const file of fs.readdirSync(DESIGN).filter((f) => f.endsWith('.svg'))) {
      const name = file.replace(/\.svg$/, '');
      const svg = fs.readFileSync(path.join(DESIGN, file), 'utf8');
      const [w, h] = size(svg);
      // 같은 폴더에 임시 HTML을 두어야 hero.svg의 ../images/v2/ 경로가 풀린다
      const tmp = path.join(DESIGN, `_bake_${name}.html`);
      fs.writeFileSync(tmp, `<!doctype html><html><head><meta charset="utf-8">${fontLink}<style>html,body{margin:0;background:transparent}svg{display:block}</style></head><body>${svg}</body></html>`);
      const page = await browser.newPage();
      await page.setViewport({ width: w, height: h, deviceScaleFactor: 2 });
      await page.goto(pathToFileURL(tmp).href, { waitUntil: 'networkidle0', timeout: 90000 });
      await page.evaluate(() => document.fonts.ready);
      const webp = WEBP.has(name);
      const out = path.join(OUT, `${name}.${webp ? 'webp' : 'png'}`);
      await page.screenshot({ path: out, type: webp ? 'webp' : 'png', ...(webp ? { quality: 88 } : {}), omitBackground: true, clip: { x: 0, y: 0, width: w, height: h } });
      await page.close();
      fs.unlinkSync(tmp);
      console.log('baked', path.relative(path.join(DESIGN, '..', '..'), out), `${w * 2}x${h * 2}`);
    }
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
