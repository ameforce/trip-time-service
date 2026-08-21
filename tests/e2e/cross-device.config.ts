import { defineConfig } from '@playwright/test';

/**
 * 크로스 브라우저(Chromium/Firefox/WebKit) × 디바이스 매트릭스 실행 설정.
 * 서버는 playwright.config.ts와 동일하게 fixture/mock 모드로 자동 기동한다.
 */

const e2ePort = process.env.E2E_PORT ?? '39081';
const baseURL = `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  // 이 config는 tests/e2e/ 안에 있으므로 상대경로는 config 파일 기준으로 해석된다.
  testDir: '.',
  testMatch: /cross-device\.spec\.ts/,
  timeout: 200_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  outputDir: '.artifacts/e2e/cross-device-results',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'firefox', use: { browserName: 'firefox' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
  webServer: {
    command: 'uv run trip-time-service',
    env: {
      TTS_PROVIDER: process.env.TTS_PROVIDER ?? 'mock',
      TTS_E2E_FIXTURE_MODE: '1',
      TTS_AUTOCOMPLETE_BROWSER_ENABLE: '0',
      TTS_PORT_STRICT: '1',
      TTS_RELOAD: 'false',
      TTS_HEADLESS: 'true',
      TTS_PORT: e2ePort,
    },
    url: `${baseURL}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
