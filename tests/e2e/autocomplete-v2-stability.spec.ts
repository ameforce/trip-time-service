import { expect, test } from '@playwright/test';

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

function autocompleteItem(name: string) {
  return {
    lat: '37.505089',
    lon: '127.004918',
    display_name: name,
    address: '서울 서초구 신반포로 176',
    type: '버스터미널',
    coords_ready: true,
    selection_kind: 'poi',
    canonical_query: name,
    source: 'test',
    confidence: 0.99,
  };
}

test.describe('autocomplete v2 stability', () => {
  test('uses stable prefix for trailing hangul jamo without mutating IME display value', async ({
    page,
  }) => {
    const autocompleteQueries: string[] = [];

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
      autocompleteQueries.push(query);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(query === '센트' ? [autocompleteItem('센트럴시티')] : []),
      });
    });

    await page.goto('/');
    await page.locator('#origin').click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#origin');
      if (!input) throw new Error('origin input missing');
      input.focus();
      input.dispatchEvent(
        new CompositionEvent('compositionstart', {
          bubbles: true,
          data: 'ㄹ',
        }),
      );
      input.value = '센트ㄹ';
      input.dispatchEvent(
        new InputEvent('input', {
          bubbles: true,
          data: 'ㄹ',
          inputType: 'insertCompositionText',
        }),
      );
    });

    await expect.poll(() => autocompleteQueries, { timeout: 3000 }).toEqual(['센트']);
    await expect(page.locator('#origin')).toHaveValue('센트ㄹ');
    await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible();

    const metrics = await page.evaluate(() => {
      const reader = (window as unknown as {
        __ttsGetAutocompleteMetrics?: () => {
          events: Array<Record<string, unknown>>;
        };
      }).__ttsGetAutocompleteMetrics;
      return reader ? reader() : null;
    });
    expect(
      metrics?.events.some(
        (event) =>
          event.stage === 'state' &&
          event.phase === 'scheduled' &&
          event.effective_query_source === 'stable-prefix',
      ),
    ).toBeTruthy();
  });

  test('single owner schedules one request for one plain input sequence', async ({ page }) => {
    const autocompleteQueries: string[] = [];

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
      autocompleteQueries.push(requestUrl.searchParams.get('q') ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([autocompleteItem('강남역')]),
      });
    });

    await page.goto('/');
    await expect(page.locator('#origin')).toBeVisible();
    const version = await page.evaluate(
      () => (window as unknown as { __ttsAutocompleteControllerVersion?: string }).__ttsAutocompleteControllerVersion,
    );
    expect(version).toBe('v2-single-owner');

    await page.locator('#origin').click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#origin');
      if (!input) throw new Error('origin input missing');
      input.value = '강남';
      input.dispatchEvent(
        new InputEvent('input', {
          bubbles: true,
          data: '남',
          inputType: 'insertText',
        }),
      );
    });

    await expect.poll(() => autocompleteQueries, { timeout: 3000 }).toEqual(['강남']);
    await page.waitForTimeout(350);
    expect(autocompleteQueries).toEqual(['강남']);
  });

  test('waits for slow live autocomplete responses before aborting', async ({ page }) => {
    const autocompleteQueries: string[] = [];

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
      autocompleteQueries.push(requestUrl.searchParams.get('q') ?? '');
      await new Promise((resolve) => setTimeout(resolve, 13_000));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          autocompleteItem('경기 수원시 팔달구 경수대로680번길 40 센트럴하우스'),
        ]),
      });
    });

    await page.goto('/');
    await expect(page.locator('#origin')).toBeVisible();
    await page.locator('#origin').fill('경수대로680번길40');

    await expect.poll(() => autocompleteQueries, { timeout: 3000 }).toEqual([
      '경수대로680번길40',
    ]);
    await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.locator('#origin-ac .ac-item').first()).toContainText(
      '센트럴하우스',
    );
  });
});
