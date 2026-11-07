/**
 * 生成前端 API 类型：后端离线导出 OpenAPI → openapi-typescript 生成 schema.d.ts
 *
 * 相比原来直接打 `http://localhost:8000/openapi.json`：
 * - 不需要先启动后端（后端用 `app.openapi()` 离线导出，不连库）；
 * - 不会因为 8000 端口被占用或后端未起而静默失败；
 * - 跨平台（Windows 下 `npm` 脚本里不方便直接调 .venv 解释器）。
 *
 * 用法：npm run gen:api
 * 后端解释器：默认 backend/.venv，可用环境变量 HS_PYTHON 覆盖。
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontendDir = resolve(here, '..')
const backendDir = resolve(frontendDir, '..', 'backend')

const isWin = process.platform === 'win32'
const venvPython = join(backendDir, '.venv', isWin ? 'Scripts/python.exe' : 'bin/python')
const python = process.env.HS_PYTHON || (existsSync(venvPython) ? venvPython : 'python')

const openapiJson = join(frontendDir, '.openapi.json')
const outFile = join(frontendDir, 'src/api/schema.d.ts')
const cliJs = join(frontendDir, 'node_modules', 'openapi-typescript', 'bin', 'cli.js')

function run(cmd, args, cwd) {
  execFileSync(cmd, args, { cwd, stdio: 'inherit' })
}

try {
  if (!existsSync(cliJs)) {
    throw new Error(`未找到 openapi-typescript，请先在 frontend/ 执行 npm install`)
  }
  mkdirSync(dirname(outFile), { recursive: true })

  console.log(`[gen:api] 导出 OpenAPI → ${openapiJson}`)
  run(python, [join(backendDir, 'scripts', 'export_openapi.py'), openapiJson], backendDir)

  console.log(`[gen:api] 生成类型 → ${outFile}`)
  run(process.execPath, [cliJs, openapiJson, '-o', outFile], frontendDir)

  console.log('[gen:api] 完成')
} finally {
  rmSync(openapiJson, { force: true })
}
