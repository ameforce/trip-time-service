import { expect, test } from '@playwright/test';

test.describe('autocomplete warmup payload', () => {
  test('prefers canonical/core queries over verbose recent and favorite text', async ({
    page,
  }) => {
    const warmupPayloads: Array<Record<string, unknown>> = [];

    await page.addInitScript(() => {
      localStorage.setItem(
        'tts_recent_searches',
        JSON.stringify([
          {
            origin: '경기 수원시 팔달구 경수대로680번길 40 센트럴하우스',
            destination: '히엘',
            origin_coords: {
              lat: null,
              lon: null,
              display_name: '경기 수원시 팔달구 경수대로680번길 40 센트럴하우스',
              address: '경기 수원시 팔달구 경수대로680번길 40 센트럴하우스',
              coords_ready: false,
              selection_kind: 'poi',
              canonical_query: '경수대로680번길 40',
            },
            destination_coords: {
              lat: null,
              lon: null,
              display_name: '히엘',
              address: '히엘',
              coords_ready: false,
              selection_kind: 'poi',
              canonical_query: '히엘',
            },
            ts: Date.now(),
          },
        ]),
      );
      localStorage.setItem(
        'tts_favorites',
        JSON.stringify([
          {
            name: '강남파이낸스센터',
            address: '서울 강남구 테헤란로 152 강남파이낸스센터',
            lat: 37.5,
            lon: 127.036,
          },
        ]),
      );
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
      const payloadText = route.request().postData() ?? '{}';
      warmupPayloads.push(JSON.parse(payloadText) as Record<string, unknown>);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ queued: 0 }),
      });
    });

    await page.goto('/');
    await expect(page.locator('#search-btn')).toBeVisible();
    await expect.poll(() => warmupPayloads.length).toBe(1);

    const payload = warmupPayloads[0];
    const queries = Array.isArray(payload.queries) ? payload.queries.map(String) : [];

    expect(queries).toContain('경수대로680번길 40');
    expect(queries).toContain('히엘');
    expect(queries).toContain('강남파이낸스센터');
    expect(queries).toContain('테헤란로 152');
    expect(queries).not.toContain('경기 수원시 팔달구 경수대로680번길 40 센트럴하우스');
    expect(queries).not.toContain('서울 강남구 테헤란로 152 강남파이낸스센터');
  });
});
