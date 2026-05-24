import { expect, test } from '@playwright/test';

import {
  assertAutocompleteInput,
  loadAutocompleteCases,
  saveCapture,
  writeJsonArtifact,
} from './live_utils';

const blockingCases = loadAutocompleteCases('blocking');
const extendedCases = loadAutocompleteCases('extended');

test.describe('live autocomplete smoke', () => {
  test('road-address regression: 경수대로680번길40', async ({ page, request }, testInfo) => {
    await page.goto('/');
    await expect(page.locator('#search-btn')).toBeVisible();
    const result = await assertAutocompleteInput(page, request, '#origin', '#origin-ac', {
      query: '경수대로680번길40',
      expected_any: ['경수대로680번길40', '경수대로680번길', '40', '센트럴하우스'],
      min_results: 1,
      require_coords: false,
      retry_once_on_empty: true,
      category: 'road-address-regression',
    });
    await saveCapture(page, testInfo.outputPath('road-address-regression-final.png'));
    writeJsonArtifact(testInfo.outputPath('road-address-regression-report.json'), {
      suite: 'autocomplete',
      phase: 'regression',
      target: 'origin',
      ...result,
    });
  });

  for (const [index, testCase] of blockingCases.entries()) {
    test(`blocking corpus: ${testCase.query}`, async ({ page, request }, testInfo) => {
      const useOrigin = index % 2 === 0;
      await page.goto('/');
      await expect(page.locator('#search-btn')).toBeVisible();
      await saveCapture(page, testInfo.outputPath(`blocking-${index + 1}-initial.png`));
      const result = await assertAutocompleteInput(
        page,
        request,
        useOrigin ? '#origin' : '#destination',
        useOrigin ? '#origin-ac' : '#dest-ac',
        testCase,
      );
      await saveCapture(page, testInfo.outputPath(`blocking-${index + 1}-dropdown.png`));
      await page.locator('#search-btn').focus();
      await saveCapture(page, testInfo.outputPath(`blocking-${index + 1}-final.png`));
      writeJsonArtifact(testInfo.outputPath(`blocking-${index + 1}-report.json`), {
        suite: 'autocomplete',
        phase: 'blocking',
        target: useOrigin ? 'origin' : 'destination',
        ...result,
      });
    });
  }

  for (const [index, testCase] of extendedCases.entries()) {
    test(`extended corpus: ${testCase.query}`, async ({ page, request }, testInfo) => {
      test.skip(!process.env.TTS_LIVE_EXTENDED, 'extended live suite disabled');
      const useOrigin = index % 2 === 0;
      await page.goto('/');
      await expect(page.locator('#search-btn')).toBeVisible();
      await saveCapture(page, testInfo.outputPath(`extended-${index + 1}-initial.png`));
      const result = await assertAutocompleteInput(
        page,
        request,
        useOrigin ? '#origin' : '#destination',
        useOrigin ? '#origin-ac' : '#dest-ac',
        testCase,
      );
      await saveCapture(page, testInfo.outputPath(`extended-${index + 1}-dropdown.png`));
      await page.locator('#search-btn').focus();
      await saveCapture(page, testInfo.outputPath(`extended-${index + 1}-final.png`));
      writeJsonArtifact(testInfo.outputPath(`extended-${index + 1}-report.json`), {
        suite: 'autocomplete',
        phase: 'extended',
        target: useOrigin ? 'origin' : 'destination',
        ...result,
      });
    });
  }
});
