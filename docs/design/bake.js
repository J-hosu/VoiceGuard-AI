// docs/design/*.svg 를 README용 그림(docs/images/readme/)으로 굽는다.
// GitHub는 <img> 속 SVG에서 웹 글꼴을 쓰지 못하므로, fonts.txt의 글꼴을 불러온 브라우저에서 2배 크기로 찍는다.
// 그림마다 밝은 화면용(<이름>.png)과 어두운 화면용(<이름>-dark.png)을 함께 만든다. 색은 theme.css에 있다.
//
//   npm i --no-save puppeteer-core
//   CHROME="<Chrome 또는 Edge 실행 파일 경로>" node docs/design/bake.js [이름 ...]
//
// 이름을 주면 그 그림만 굽는다(예: node docs/design/bake.js numbers loop).
// hero는 실제 화면 두 장(../images/v2/)을 합성한 사진 같은 그림이라 WebP로, 나머지는 PNG로 저장한다.
'use strict';
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const puppeteer = require('puppeteer-core');

const DESIGN = __dirname;
const OUT = path.join(DESIGN, '..', 'images', 'readme');
const WEBP = new Set(['hero']);
const THEMES = ['light', 'dark'];
const CHROME = process.env.CHROME;
if (!CHROME) {
  console.error('CHROME 환경 변수에 Chrome 또는 Edge 실행 파일 경로를 넣어 주세요.');
  process.exit(1);
}

const only = new Set(process.argv.slice(2));
const families = fs
  .readFileSync(path.join(DESIGN, 'fonts.txt'), 'utf8')
  .split(/\r?\n/)
  .map((s) => s.trim())
  .filter(Boolean);
const fontLink = `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?${families.map((f) => 'family=' + f).join('&')}&display=block">`;
const themeCss = fs.readFileSync(path.join(DESIGN, 'theme.css'), 'utf8');

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
      if (only.size && !only.has(name)) continue;
      // SVG를 단독으로 열 때 쓰는 스타일시트 지시문은 HTML 안에서는 필요 없다
      const svg = fs.readFileSync(path.join(DESIGN, file), 'utf8').replace(/<\?xml[^>]*\?>\s*/g, '');
      const [w, h] = size(svg);
      // 같은 폴더에 임시 HTML을 두어야 hero.svg의 ../images/v2/ 경로가 풀린다
      const tmp = path.join(DESIGN, `_bake_${name}.html`);
      fs.writeFileSync(tmp, `<!doctype html><html><head><meta charset="utf-8">${fontLink}<style>${themeCss}\nhtml,body{margin:0;background:transparent}svg{display:block}</style></head><body>${svg}</body></html>`);
      const page = await browser.newPage();
      await page.setViewport({ width: w, height: h, deviceScaleFactor: 2 });
      await page.goto(pathToFileURL(tmp).href, { waitUntil: 'networkidle0', timeout: 90000 });
      await page.evaluate(() => document.fonts.ready);
      const webp = WEBP.has(name);
      for (const theme of THEMES) {
        await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
        const out = path.join(OUT, `${name}${theme === 'dark' ? '-dark' : ''}.${webp ? 'webp' : 'png'}`);
        await page.screenshot({ path: out, type: webp ? 'webp' : 'png', ...(webp ? { quality: 88 } : {}), omitBackground: true, clip: { x: 0, y: 0, width: w, height: h } });
        console.log('baked', path.relative(path.join(DESIGN, '..', '..'), out), `${w * 2}x${h * 2}`);
      }
      await page.close();
      fs.unlinkSync(tmp);
    }
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
