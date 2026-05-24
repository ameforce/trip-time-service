import { expect, test } from '@playwright/test';

import {
  loadRouteCases,
  runRouteScenario,
  saveCapture,
  writeJsonArtifact,
} from './live_utils';

const blockingScenarios = loadRouteCases('blocking').filter((item) => item.mode === 'arrival');
const extendedScenarios = loadRouteCases('extended').filter((item) => item.mode === 'arrival');

test.describe('live arrival mode smoke', () => {
  for (const [index, scenario] of blockingScenarios.entries()) {
    test(
      `blocking arrival: ${scenario.origin_query} -> ${scenario.destination_query}`,
      async ({ page, request }, testInfo) => {
        await page.goto('/');
        await expect(page.locator('#search-btn')).toBeVisible();
        await saveCapture(page, testInfo.outputPath(`arrival-blocking-${index + 1}-initial.png`));
        const result = await runRouteScenario(page, request, scenario, {
          preSearchPath: testInfo.outputPath(`arrival-blocking-${index + 1}-ready.png`),
        });
        await saveCapture(page, testInfo.outputPath(`arrival-blocking-${index + 1}-final.png`));
        writeJsonArtifact(
          testInfo.outputPath(`arrival-blocking-${index + 1}-report.json`),
          {
            suite: 'arrival-route',
            phase: 'blocking',
            ...result,
          },
        );
      },
    );
  }

  for (const [index, scenario] of extendedScenarios.entries()) {
    test(
      `extended arrival: ${scenario.origin_query} -> ${scenario.destination_query}`,
      async ({ page, request }, testInfo) => {
        test.skip(!process.env.TTS_LIVE_EXTENDED, 'extended live suite disabled');
        await page.goto('/');
        await expect(page.locator('#search-btn')).toBeVisible();
        await saveCapture(page, testInfo.outputPath(`arrival-extended-${index + 1}-initial.png`));
        const result = await runRouteScenario(page, request, scenario, {
          preSearchPath: testInfo.outputPath(`arrival-extended-${index + 1}-ready.png`),
        });
        await saveCapture(page, testInfo.outputPath(`arrival-extended-${index + 1}-final.png`));
        writeJsonArtifact(
          testInfo.outputPath(`arrival-extended-${index + 1}-report.json`),
          {
            suite: 'arrival-route',
            phase: 'extended',
            ...result,
          },
        );
      },
    );
  }
});
