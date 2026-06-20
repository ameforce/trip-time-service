import { expect, test } from '@playwright/test';
import fs from 'fs';
import path from 'path';

type DatasetCase = {
  id: string;
  category: 'trailing_jamo' | 'compositionend' | 'plain_input';
  display_value: string;
  composition_value?: string;
  expected_query: string;
  response_name: string;
};

const datasetPath = path.join(process.cwd(), 'tests/fixtures/autocomplete-v2-dataset.json');
const dataset = JSON.parse(fs.readFileSync(datasetPath, 'utf-8')) as {
  case_count: number;
  cases: DatasetCase[];
};

function installQuietWarmup(page: import('@playwright/test').Page) {
  return page.addInitScript(() => {
    localStorage.setItem('tts_recent_searches', '[]');
    localStorage.setItem('tts_favorites', '[]');
    window.requestIdleCallback = (callback: IdleRequestCallback) => {
      window.setTimeout(
        () =>
          callback({
            didTimeout: false,
            timeRemaining: () => 50,
          } as IdleDeadline),
        0,
      );
      return 1;
    };
  });
}

function responseItem(name: string, query: string) {
  return {
    lat: '37.505089',
    lon: '127.004918',
    display_name: name,
    address: `서울 테스트로 ${query}`,
    type: '장소',
    coords_ready: true,
    selection_kind: 'poi',
    canonical_query: query,
    source: 'dataset',
    confidence: 0.99,
  };
}

async function dispatchDatasetCase(
  page: import('@playwright/test').Page,
  testCase: DatasetCase,
) {
  await page.evaluate((currentCase) => {
    const input = document.querySelector<HTMLInputElement>('#origin');
    if (!input) throw new Error('origin input missing');
    input.focus();
    input.value = '';
    input.dispatchEvent(
      new InputEvent('input', {
        bubbles: true,
        inputType: 'deleteContentBackward',
      }),
    );
    if (currentCase.category === 'trailing_jamo') {
      input.dispatchEvent(
        new CompositionEvent('compositionstart', {
          bubbles: true,
          data: currentCase.display_value.slice(-1),
        }),
      );
      input.value = currentCase.display_value;
      input.dispatchEvent(
        new InputEvent('input', {
          bubbles: true,
          data: currentCase.display_value.slice(-1),
          inputType: 'insertCompositionText',
        }),
      );
      return;
    }
    if (currentCase.category === 'compositionend') {
      input.dispatchEvent(
        new CompositionEvent('compositionstart', {
          bubbles: true,
          data: currentCase.composition_value ?? '',
        }),
      );
      input.value = currentCase.composition_value ?? currentCase.display_value;
      input.dispatchEvent(
        new CompositionEvent('compositionend', {
          bubbles: true,
          data: currentCase.display_value,
        }),
      );
      input.value = currentCase.display_value;
      return;
    }
    input.value = currentCase.display_value;
    input.dispatchEvent(
      new InputEvent('input', {
        bubbles: true,
        data: currentCase.display_value,
        inputType: 'insertText',
      }),
    );
  }, testCase);
}

test.describe('autocomplete v2 generated dataset', () => {
  test('runs hundreds of IME/plain autocomplete cases through one controller', async ({
    page,
  }) => {
    expect(dataset.case_count).toBe(dataset.cases.length);
    expect(dataset.cases.length).toBeGreaterThanOrEqual(300);

    const requests: Array<{ id: string; query: string }> = [];
    const seenIds = new Set<string>();

    await installQuietWarmup(page);
    await page.route('**/api/autocomplete/warmup', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ queued: 0 }),
      });
    });
    await page.route('**/api/autocomplete?*', async (route) => {
      const requestUrl = new URL(route.request().url());
      const query = requestUrl.searchParams.get('q') ?? '';
      const matched = dataset.cases.find(
        (candidate) => candidate.expected_query === query,
      );
      requests.push({ id: matched?.id ?? 'unknown', query });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { 'X-TTS-Autocomplete-Count': '1' },
        body: JSON.stringify([
          responseItem(matched?.response_name ?? `${query} 후보`, query),
        ]),
      });
    });

    await page.goto('/');
    await expect(page.locator('#origin')).toBeVisible();
    await page.locator('#origin').click();
    const version = await page.evaluate(
      () =>
        (window as unknown as { __ttsAutocompleteControllerVersion?: string })
          .__ttsAutocompleteControllerVersion,
    );
    expect(version).toBe('v2-single-owner');

    for (let index = 0; index < dataset.cases.length; index += 1) {
      const testCase = dataset.cases[index];
      const beforeCount = requests.length;
      await dispatchDatasetCase(page, testCase);
      await expect
        .poll(() => requests.length, {
          timeout: 3000,
          message: `request count for ${testCase.id}`,
        })
        .toBe(beforeCount + 1);
      expect(requests.at(-1), testCase.id).toEqual({
        id: testCase.id,
        query: testCase.expected_query,
      });
      seenIds.add(testCase.id);
      const first = page.locator('#origin-ac .ac-item').first();
      await expect(first, testCase.id).toBeVisible({ timeout: 1500 });
      await expect(first, testCase.id).toContainText(testCase.response_name);
      if (testCase.category === 'trailing_jamo') {
        await expect(page.locator('#origin'), testCase.id).toHaveValue(
          testCase.display_value,
        );
      }
    }

    await page.waitForTimeout(500);
    expect(seenIds.size).toBe(dataset.cases.length);
    expect(requests.length).toBe(dataset.cases.length);
  });
});
