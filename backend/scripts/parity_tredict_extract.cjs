/**
 * V5 对拍基准：从 tredict-upload.js **原样抽取**构造函数并在沙箱中执行，
 * 输出 buildHrvRecords / buildSleepRecords / buildBodyvalueRecords 的结果 JSON。
 *
 * 关键：函数体从源文件按大括号配对提取，不做任何改写 → 保证基准即"行为规范"本身。
 *
 * 用法：node scripts/parity_tredict_extract.cjs <源文件路径> <输出JSON路径>
 */
'use strict';
const fs = require('node:fs');

const SRC = process.argv[2];
const OUT = process.argv[3];
if (!SRC || !OUT) {
  console.error('用法: node parity_tredict_extract.cjs <src.js> <out.json>');
  process.exit(2);
}

const src = fs.readFileSync(SRC, 'utf8');

function grab(name) {
  const re = new RegExp(`^function ${name}\\s*\\(`, 'm');
  const m = re.exec(src);
  if (!m) throw new Error('未找到函数: ' + name);
  const braceStart = src.indexOf('{', m.index);
  let depth = 0;
  let j = braceStart;
  for (; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') {
      depth--;
      if (depth === 0) {
        j++;
        break;
      }
    }
  }
  return src.slice(m.index, j);
}

const NAMES = [
  'round1',
  'wallToIso',
  'wallMs',
  'msWallToIso',
  'midpointIso',
  'ymd',
  'normTs',
  'dataMap',
  'buildHrvRecords',
  'buildSleepRecords',
  'buildBodyvalueRecords',
];

// 依赖常量（与源文件配置段一致）
const consts = [
  'const ROOT = ' + JSON.stringify(require('node:path').resolve(require('node:path').dirname(SRC), '..')) + ';',
  "const OUT_DIR = require('node:path').join(ROOT, 'garmin_data');",
  "const HRV_FILE = require('node:path').join(OUT_DIR, 'hrv', 'hrv.json');",
  "const SLEEP_FILE = require('node:path').join(OUT_DIR, 'sleep', 'sleep.json');",
  "const DAILY_FILE = require('node:path').join(OUT_DIR, 'daily', 'daily_summary.json');",
  "const WEIGHT_FILE = require('node:path').join(OUT_DIR, 'body_composition', 'weight.json');",
  'const TZ_OFFSET_MS = 8 * 3600 * 1000;',
  'const HEIGHT_CM = 170;',
].join('\n');

const readJson = (f, d) => {
  try {
    return JSON.parse(fs.readFileSync(f, 'utf8'));
  } catch {
    return d;
  }
};

const body = consts + '\n' + NAMES.map(grab).join('\n') +
  '\nreturn { buildHrvRecords, buildSleepRecords, buildBodyvalueRecords };';

const factory = new Function('fs', 'require', 'readJson', body);
const api = factory(fs, require, readJson);

const nowMs = Date.now();
const hrv = api.buildHrvRecords();
const sleep = api.buildSleepRecords();
const bv = api.buildBodyvalueRecords();

fs.writeFileSync(
  OUT,
  JSON.stringify({ nowMs, hrv, sleep, bodyvalues: bv }, null, 0),
  'utf8'
);
console.log('基准已生成 ->', OUT);
console.log('hrv=%d sleep=%d bodyvalues=%d', hrv.records.length, sleep.records.length, bv.records.length);
