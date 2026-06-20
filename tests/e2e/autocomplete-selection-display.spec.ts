import { expect, test } from '@playwright/test';

function buildSseBody(events: Array<{ event: string; data: unknown }>): string {
  return events
    .map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join('');
}

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

test.describe('autocomplete unresolved poi display', () => {
  test('geocodes clicked unresolved poi candidate and shows a map marker', async ({
    page,
  }) => {
    const geocodeQueries: string[] = [];
    const unresolvedPoi = {
      lat: null,
      lon: null,
      display_name: '경기 수원시 팔달구 경수대로680번길 40 센트럴하우스',
      address: '경기 수원시 팔달구 경수대로680번길 40 센트럴하우스',
      type: '장소',
      source: 'naver_browser_suggest',
      confidence: 0.9,
      coords_ready: false,
      selection_kind: 'poi',
      canonical_query: '경수대로680번길 40',
    };

    await page.route('**/api/autocomplete**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/autocomplete/warmup') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ queued: 0 }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          url.searchParams.get('q') === '경수대로680번길40' ? [unresolvedPoi] : [],
        ),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      geocodeQueries.push(url.searchParams.get('q') ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            lat: 37.277501,
            lon: 127.030501,
            source: 'naver_browser_geocode',
            confidence: 0.95,
          },
        ]),
      });
    });

    await page.goto('/');
    await page.fill('#origin', '경수대로680번길40');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();

    await expect(page.locator('#origin')).toHaveValue('경수대로680번길 40');
    await expect
      .poll(() => geocodeQueries, { timeout: 3000 })
      .toEqual(['경수대로680번길 40']);
    await expect
      .poll(async () => page.locator('.leaflet-marker-icon').count(), {
        timeout: 5000,
      })
      .toBeGreaterThanOrEqual(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
  });

  test('keeps poi label but blocks stream request when coordinates stay unresolved', async ({
    page,
  }) => {
    const baselineRequests: Array<Record<string, unknown>> = [];
    const streamRequests: Array<Record<string, unknown>> = [];
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

    await page.route('**/api/autocomplete**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/autocomplete/warmup') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ queued: 0 }),
        });
        return;
      }
      const query = url.searchParams.get('q') ?? '';
      const items =
        query === '서울역'
          ? [
              {
                lat: 37.554722,
                lon: 126.970833,
                display_name: '서울역',
                address: '서울 중구 한강대로 405',
                type: '역',
                source: 'naver_map',
                confidence: 0.98,
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

    const geocodeQueries: string[] = [];
    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      geocodeQueries.push(url.searchParams.get('q') ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.route('**/v1/trip/recommended-departure-time/stream', async (route) => {
      const payload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      streamRequests.push(payload);
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([
          {
            event: 'plan',
            data: {
              checked: 0,
              planned: 1,
              remaining: 1,
              total_candidates: 1,
            },
          },
          {
            event: 'recommendation',
            data: {
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
            },
          },
        ]),
      });
    });

    await page.goto('/');
    await page.click('#tab-departure');
    await page.fill('#datetime-input', futureLocalDatetime('06:30'));

    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();

    await page.fill('#destination', '히엘');
    await expect(page.locator('#dest-ac .ac-item')).toHaveCount(1);
    await page.locator('#dest-ac .ac-item').first().click();

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
