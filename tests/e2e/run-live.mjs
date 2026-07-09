import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

import { writeLiveSummary } from './live-summary.mjs';

const LIVE_TARGETS = {
  smoke: [
    'tests/e2e/autocomplete.spec.ts',
    'tests/e2e/arrival-mode.spec.ts',
    'tests/e2e/departure-mode.spec.ts',
    'tests/e2e/road-address-route.spec.ts',
  ],
  diagnose: [
    'tests/e2e/autocomplete.spec.ts',
    'tests/e2e/autocomplete-ime.spec.ts',
    'tests/e2e/autocomplete-selection-display.spec.ts',
    'tests/e2e/road-address-route.spec.ts',
  ],
  extended: [],
};

const mode = process.argv[2] || 'smoke';
if (!Object.hasOwn(LIVE_TARGETS, mode)) {
  console.error(`Unknown live E2E mode: ${mode}`);
  process.exit(2);
}
const passThroughArgs = process.argv.slice(3);
const listOnly = passThroughArgs.includes('--list');

const artifactRoot = resolve(
  listOnly ? `.artifacts/live-list/${mode}` : '.artifacts/live',
);
const env = {
  ...process.env,
  TTS_PROVIDER: 'naver_playwright',
  TTS_E2E_FIXTURE_MODE: '0',
  TTS_PORT_STRICT: '1',
  TTS_LIVE_MODE: mode,
  TTS_E2E_ARTIFACTS_DIR: artifactRoot,
};
if (mode === 'extended') {
  env.TTS_LIVE_EXTENDED = '1';
}

const playwrightBin =
  process.platform === 'win32'
    ? resolve('node_modules/.bin/playwright.cmd')
    : resolve('node_modules/.bin/playwright');
const result = spawnSync(
  playwrightBin,
  ['test', ...LIVE_TARGETS[mode], ...passThroughArgs],
  {
    env,
    stdio: 'inherit',
  },
);

if (!listOnly) {
  writeLiveSummary(artifactRoot);
}

if (result.signal) {
  console.error(`Live E2E terminated by signal: ${result.signal}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
