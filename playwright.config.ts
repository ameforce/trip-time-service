import { defineConfig } from '@playwright/test';

const e2ePort = process.env.E2E_PORT ?? '39080';
const baseURL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 120_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: '.artifacts/e2e/playwright-report', open: 'never' }],
  ],
  outputDir: '.artifacts/e2e/test-results',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command:
      `cmd /c "set TTS_PROVIDER=mock&&` +
      `set TTS_RELOAD=false&&` +
      `set TTS_PORT=${e2ePort}&&` +
      'uv run trip-time-service"',
    url: `${baseURL}/healthz`,
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
