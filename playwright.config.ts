import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { defineConfig } from '@playwright/test';

const e2ePort = process.env.E2E_PORT ?? '39080';
const baseURL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${e2ePort}`;
const e2eDebugToken =
  process.env.TTS_E2E_DEBUG_TOKEN ?? 'playwright-e2e-debug-token';
const e2eProvider = process.env.TTS_PROVIDER ?? 'mock';
const strictPort = process.env.TTS_PORT_STRICT ?? '1';
const fixtureMode = process.env.TTS_E2E_FIXTURE_MODE ?? '1';
const autocompleteBrowserEnable =
  process.env.TTS_AUTOCOMPLETE_BROWSER_ENABLE ?? (fixtureMode === '1' ? '0' : '1');
const browserExecutablePath = process.env.TTS_E2E_BROWSER_EXECUTABLE_PATH?.trim();
const liveMode = fixtureMode !== '1' && e2eProvider !== 'mock';
const liveArtifactsDir =
  process.env.TTS_E2E_ARTIFACTS_DIR ?? join(process.cwd(), '.artifacts', 'live');
mkdirSync(liveArtifactsDir, { recursive: true });
process.env.E2E_BASE_URL = baseURL;
process.env.TTS_E2E_ARTIFACTS_DIR = liveArtifactsDir;
process.env.TTS_E2E_DEBUG_TOKEN = e2eDebugToken;
process.env.TTS_PROVIDER = e2eProvider;
process.env.TTS_PORT_STRICT = strictPort;
process.env.TTS_E2E_FIXTURE_MODE = fixtureMode;
process.env.TTS_AUTOCOMPLETE_BROWSER_ENABLE = autocompleteBrowserEnable;
const chromeUserDataDir =
  process.env.TTS_E2E_CHROME_USER_DATA_DIR ??
  mkdtempSync(join(liveArtifactsDir, 'chrome-profile-'));
process.env.TTS_E2E_CHROME_USER_DATA_DIR = chromeUserDataDir;
writeFileSync(
  resolve(liveArtifactsDir, 'chrome-user-data-dir.txt'),
  `${chromeUserDataDir}\n`,
  'utf-8',
);
writeFileSync(
  resolve(liveArtifactsDir, 'e2e-runtime.json'),
  `${JSON.stringify(
    {
      baseURL,
      E2E_PORT: e2ePort,
      strict: strictPort === '1',
      fixture: fixtureMode === '1',
      live: liveMode,
      TTS_PROVIDER: e2eProvider,
      TTS_E2E_FIXTURE_MODE: fixtureMode,
      TTS_AUTOCOMPLETE_BROWSER_ENABLE: autocompleteBrowserEnable,
      TTS_LIVE_MODE: process.env.TTS_LIVE_MODE ?? '',
      LIVE_E2E_POLICY: process.env.LIVE_E2E_POLICY ?? '',
    },
    null,
    2,
  )}\n`,
  'utf-8',
);
const serverEnv = {
  TTS_PROVIDER: e2eProvider,
  TTS_E2E_FIXTURE_MODE: fixtureMode,
  TTS_AUTOCOMPLETE_BROWSER_ENABLE: autocompleteBrowserEnable,
  TTS_PORT_STRICT: strictPort,
  TTS_RELOAD: 'false',
  TTS_HEADLESS: 'true',
  TTS_ENABLE_DEBUG_ROUTES: '1',
  TTS_DEBUG_TOKEN: e2eDebugToken,
  TTS_PORT: e2ePort,
  TTS_CHROME_USER_DATA_DIR: chromeUserDataDir,
};

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 240_000,
  expect: {
    timeout: 15_000,
  },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: '.artifacts/live/playwright-report', open: 'never' }],
  ],
  outputDir: '.artifacts/live/test-results',
  globalTeardown: './tests/e2e/global-teardown.ts',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: browserExecutablePath ? 'off' : 'retain-on-failure',
    ...(browserExecutablePath
      ? {
          launchOptions: {
            executablePath: browserExecutablePath,
          },
        }
      : {}),
  },
  webServer: {
    command: 'uv run trip-time-service',
    env: serverEnv,
    url: `${baseURL}/healthz`,
    reuseExistingServer: false,
    timeout: 240_000,
  },
});
