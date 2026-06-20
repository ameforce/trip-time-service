import { expect, test } from '@playwright/test';

function futureLocalDatetime(hhmm: string): string {
  const [hoursText, minutesText] = hhmm.split(':');
  const current = new Date();
  current.setSeconds(0, 0);
  current.setDate(current.getDate() + 7);
  current.setHours(Number(hoursText), Number(minutesText), 0, 0);
  const yyyy = String(current.getFullYear());
  const mm = String(current.getMonth() + 1).padStart(2, '0');
  const dd = String(current.getDate()).padStart(2, '0');
  const hh = String(current.getHours()).padStart(2, '0');
  const mi = String(current.getMinutes()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}T${hh}:${mi}`;
}

test.describe('autocomplete ime composition', () => {
  test('keeps showing suggestions while korean composition is active with stable syllables', async ({
    page,
  }) => {
    const autocompleteQueries: string[] = [];

    await page.addInitScript(() => {
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
        body: JSON.stringify([
          {
            lat: '37.505089',
            lon: '127.004918',
            display_name: '센트럴시티',
            address: '서울 서초구 신반포로 176',
            type: '버스터미널',
            coords_ready: true,
            selection_kind: 'poi',
            canonical_query: '센트럴시티',
            source: 'test',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.goto('/');
    await expect(page.locator('#origin')).toBeVisible();

    await page.locator('#origin').click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#origin');
      if (!input) {
        throw new Error('origin input missing');
      }
      input.focus();
      input.dispatchEvent(
        new CompositionEvent('compositionstart', {
          bubbles: true,
          data: '센',
        }),
      );
      input.value = '센트';
      input.dispatchEvent(
        new InputEvent('input', {
          bubbles: true,
          data: '트',
          inputType: 'insertCompositionText',
        }),
      );
    });

    await expect
      .poll(() => autocompleteQueries, { timeout: 3000 })
      .toEqual(['센트']);
    await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible();
    const metrics = await page.evaluate(() => {
      const reader = (window as unknown as {
        __ttsGetAutocompleteMetrics?: () => {
          events: Array<Record<string, unknown>>;
          counters: Record<string, number>;
        };
      }).__ttsGetAutocompleteMetrics;
      return reader ? reader() : null;
    });
    expect(metrics?.events.some((event) => event.stage === 'state' && event.phase === 'rendered')).toBeTruthy();
    expect(JSON.stringify(metrics)).not.toContain('센트');
  });

  test('shows suggestions after compositionend without extra keypress', async ({
    page,
  }) => {
    const autocompleteQueries: string[] = [];

    await page.addInitScript(() => {
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
        body: JSON.stringify([
          {
            lat: '37.505089',
            lon: '127.004918',
            display_name: '센트럴시티',
            address: '서울 서초구 신반포로 176',
            type: '버스터미널',
            coords_ready: true,
            selection_kind: 'poi',
            canonical_query: '센트럴시티',
            source: 'test',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.goto('/');
    await expect(page.locator('#origin')).toBeVisible();

    await page.locator('#origin').click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#origin');
      if (!input) {
        throw new Error('origin input missing');
      }
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
      input.dispatchEvent(
        new CompositionEvent('compositionend', {
          bubbles: true,
          data: '럴',
        }),
      );
      // Simulate IME engines that commit the final string after compositionend.
      input.value = '센트럴';
    });

    await expect
      .poll(() => autocompleteQueries, { timeout: 3000 })
      .toEqual(['센트럴']);
    await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible();
  });

  test('normalizes decomposed hangul jamo to stable syllables without extra keypress', async ({
    page,
  }) => {
    const autocompleteQueries: string[] = [];

    await page.addInitScript(() => {
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
        body: JSON.stringify([
          {
            lat: '37.505089',
            lon: '127.004918',
            display_name: '센트럴시티',
            address: '서울 서초구 신반포로 176',
            type: '버스터미널',
            coords_ready: true,
            selection_kind: 'poi',
            canonical_query: '센트럴시티',
            source: 'test',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.goto('/');
    await expect(page.locator('#origin')).toBeVisible();

    await page.locator('#origin').click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#origin');
      if (!input) {
        throw new Error('origin input missing');
      }
      input.focus();
      input.dispatchEvent(
        new CompositionEvent('compositionstart', {
          bubbles: true,
          data: 'ᄉ',
        }),
      );
      input.value = '센트';
      input.dispatchEvent(
        new InputEvent('input', {
          bubbles: true,
          data: 'ᅳ',
          inputType: 'insertCompositionText',
        }),
      );
    });

    await expect
      .poll(() => autocompleteQueries, { timeout: 3000 })
      .toEqual(['센트']);
    await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible();
  });

  test.describe('mobile viewport', () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true });

    test('shows suggestions after korean compositionend without extra keypress', async ({
      page,
    }) => {
      const autocompleteQueries: string[] = [];

      await page.addInitScript(() => {
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
          body: JSON.stringify([
            {
              lat: '37.505089',
              lon: '127.004918',
              display_name: '센트럴시티',
              address: '서울 서초구 신반포로 176',
              type: '버스터미널',
              coords_ready: true,
              selection_kind: 'poi',
              canonical_query: '센트럴시티',
              source: 'test',
              confidence: 0.99,
            },
          ]),
        });
      });

      await page.goto('/');
      await page.locator('#mobile-toggle').click();
      await expect(page.locator('#sidebar')).toHaveClass(/open/);
      await expect(page.locator('#origin')).toBeVisible();

      await page.locator('#origin').click();
      await page.evaluate(() => {
        const input = document.querySelector<HTMLInputElement>('#origin');
        if (!input) {
          throw new Error('origin input missing');
        }
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
        input.dispatchEvent(
          new CompositionEvent('compositionend', {
            bubbles: true,
            data: '럴',
          }),
        );
        input.value = '센트럴';
      });

      await expect
        .poll(() => autocompleteQueries, { timeout: 3000 })
        .toEqual(['센트럴']);
      await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible();
    });
  });

  test('blocks unresolved poi route submit after compositionend fires post-click', async ({
    page,
  }) => {
    const baselineRequests: Array<Record<string, unknown>> = [];
    const streamRequests: Array<Record<string, unknown>> = [];
    const geocodeQueries: string[] = [];
    const unresolvedPoi = {
      lat: null,
      lon: null,
      display_name: '히엘',
      address: '서울 강남구 삼성로 766 유림빌딩 3층 히엘',
      type: '미용실',
      source: 'naver_browser_suggest',
      confidence: 0.9,
      coords_ready: false,
      selection_kind: 'poi',
      canonical_query: '삼성로 766',
    };

    await page.addInitScript(() => {
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
      const items =
        query === '서울역'
          ? [
              {
                lat: 37.554722,
                lon: 126.970833,
                display_name: '서울역',
                address: '서울 중구 한강대로 405',
                type: '역',
                source: 'test',
                confidence: 0.99,
                coords_ready: true,
                selection_kind: 'station',
                canonical_query: '서울역',
              },
            ]
          : query === '히엘'
            ? [unresolvedPoi]
            : [];
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(items),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const requestUrl = new URL(route.request().url());
      geocodeQueries.push(requestUrl.searchParams.get('q') ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      const payload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      baselineRequests.push(payload);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: {
            origin: payload.origin,
            destination: payload.destination,
          },
          departure_time: '2026-04-27T05:30:00+09:00',
          arrival_time: '2026-04-27T06:10:00+09:00',
          duration_seconds: 2400,
          provider: 'naver_selenium',
          cache_hit: false,
        }),
      });
    });

    await page.route('**/v1/trip/recommended-departure-time/stream', async (route) => {
      const payload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      streamRequests.push(payload);
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body:
          `event: plan\ndata: ${JSON.stringify({
            checked: 0,
            planned: 1,
            remaining: 1,
            total_candidates: 1,
          })}\n\n` +
          `event: recommendation\ndata: ${JSON.stringify({
            route: {
              origin: payload.origin,
              destination: payload.destination,
            },
            desired_arrival_time: '2026-04-27T06:30:00+09:00',
            recommended_departure_time: '2026-04-27T05:40:00+09:00',
            expected_arrival_time: '2026-04-27T06:20:00+09:00',
            duration_seconds: 2400,
            meets_deadline: true,
            provider: 'naver_selenium',
            provider_calls: 1,
            candidates_checked: 1,
            planned_queries: 1,
            total_candidates: 1,
            latest_departure_time: '2026-04-27T05:50:00+09:00',
            latest_departure_arrival_time: '2026-04-27T06:30:00+09:00',
            latest_departure_duration_seconds: 2400,
            safe_departure_time: '2026-04-27T05:35:00+09:00',
            safe_departure_duration_seconds: 3000,
            recommended_score_total: 0.91,
            baseline_score_total: 0.83,
            candidate_evaluations: [],
          })}\n\n`,
      });
    });

    await page.goto('/');
    await page.click('#tab-departure');
    await page.fill('#datetime-input', futureLocalDatetime('06:30'));

    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item').first()).toBeVisible();
    await page.locator('#origin-ac .ac-item').first().click();

    await page.locator('#destination').click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#destination');
      if (!input) {
        throw new Error('destination input missing');
      }
      input.focus();
      input.dispatchEvent(
        new CompositionEvent('compositionstart', {
          bubbles: true,
          data: '히',
        }),
      );
      input.value = '히엘';
      input.dispatchEvent(
        new InputEvent('input', {
          bubbles: true,
          data: '엘',
          inputType: 'insertCompositionText',
        }),
      );
    });

    await expect(page.locator('#dest-ac .ac-item').first()).toBeVisible();
    await page.locator('#dest-ac .ac-item').first().click();
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('#destination');
      if (!input) {
        throw new Error('destination input missing');
      }
      input.dispatchEvent(
        new CompositionEvent('compositionend', {
          bubbles: true,
          data: '히엘',
        }),
      );
    });

    await expect(page.locator('#destination')).toHaveValue(
      '히엘 (서울 강남구 삼성로 766 유림빌딩 3층 히엘)',
    );

    await page.click('#search-btn');

    await expect(page.locator('#error-box')).toContainText(
      '좌표를 확인할 수 없어 경로를 조회할 수 없습니다. 다른 후보를 선택해 주세요.',
    );
    await expect
      .poll(() => ({
        baseline: baselineRequests.length,
        stream: streamRequests.length,
      }))
      .toEqual({ baseline: 0, stream: 0 });
    expect(geocodeQueries).toContain('삼성로 766');
    expect(geocodeQueries.every((query) => query === '삼성로 766')).toBeTruthy();
  });
});
