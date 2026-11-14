// 웹 빌드(dist) 후처리: 홈 화면에 추가했을 때 앱처럼 열리도록 PWA 정보를 넣는다.
//  - 아이콘(192/512, 아이폰용 180)을 assets/icon.png에서 만들어 dist에 넣는다.
//  - manifest.json을 만든다(전체 화면, 세로 고정, 테마 색).
//  - index.html <head>에 manifest·아이콘·아이폰 전용 태그를 넣는다.
// 실행: npm run build:web  (expo export -p web 다음에 자동 실행)
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const Jimp = require('jimp-compact');

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dist = join(root, 'dist');
const APP_NAME = 'VoiceGuard AI';
const SHORT_NAME = 'VoiceGuard';
const THEME = '#0B1220';

const sizes = [
  ['icon-192.png', 192],
  ['icon-512.png', 512],
  ['apple-touch-icon.png', 180],
];
for (const [name, size] of sizes) {
  const image = await Jimp.read(join(root, 'assets', 'icon.png'));
  await image.resize(size, size).writeAsync(join(dist, name));
}

const manifest = {
  name: APP_NAME,
  short_name: SHORT_NAME,
  description: '통화 중 보이스피싱을 실시간으로 탐지하고 대응을 안내합니다.',
  lang: 'ko',
  start_url: '/',
  scope: '/',
  display: 'standalone',
  orientation: 'portrait',
  background_color: THEME,
  theme_color: THEME,
  icons: [
    { src: '/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
    { src: '/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
  ],
};
writeFileSync(join(dist, 'manifest.json'), JSON.stringify(manifest, null, 2));

const head = [
  '<link rel="manifest" href="/manifest.json" />',
  `<meta name="theme-color" content="${THEME}" />`,
  '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />',
  '<meta name="apple-mobile-web-app-capable" content="yes" />',
  '<meta name="mobile-web-app-capable" content="yes" />',
  `<meta name="apple-mobile-web-app-title" content="${SHORT_NAME}" />`,
  '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />',
  `<style>html,body{background:${THEME};overscroll-behavior:none}</style>`,
].join('\n    ');

const htmlPath = join(dist, 'index.html');
let html = readFileSync(htmlPath, 'utf8');
html = html
  .replace('<html lang="en">', '<html lang="ko">')
  .replace(
    /<meta name="viewport"[^>]*>/,
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />',
  )
  .replace('</head>', `  ${head}\n  </head>`);
writeFileSync(htmlPath, html);
console.log('PWA 후처리 완료:', sizes.map(([name]) => name).join(', '), 'manifest.json, index.html');
