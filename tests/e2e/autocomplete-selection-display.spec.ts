import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

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

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

test.describe('autocomplete unresolved poi display', () => {
  test('keeps resolved origin marker while destination input is edited', async ({
    page,
  }) => {
    const origin = {
      lat: 37.554722,
      lon: 126.970833,
      display_name: '서울역',
      address: '서울 중구 한강대로 405',
      type: '역',
      source: 'naver_map',
      confidence: 0.99,
      coords_ready: true,
      selection_kind: 'station',
      canonical_query: '서울역',
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
        body: JSON.stringify(url.searchParams.get('q') === '서울역' ? [origin] : []),
      });
    });

    await page.goto('/');
    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');

    await page.fill('#destination', '광교역');

    await expect(page.locator('#destination')).toHaveValue('광교역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
  });

  test('keeps resolved origin marker when unresolved destination candidate is clicked', async ({
    page,
  }) => {
    const geocodeQueries: string[] = [];
    const origin = {
      lat: 37.554722,
      lon: 126.970833,
      display_name: '서울역',
      address: '서울 중구 한강대로 405',
      type: '역',
      source: 'naver_map',
      confidence: 0.99,
      coords_ready: true,
      selection_kind: 'station',
      canonical_query: '서울역',
    };
    const unresolvedDestination = {
      lat: null,
      lon: null,
      display_name: '광교역(경기대)1번출구',
      address: '경기 수원시 영통구 대학로 55',
      type: '출입구',
      source: 'naver_browser_suggest',
      confidence: 0.9,
      coords_ready: false,
      selection_kind: 'poi',
      canonical_query: '광교역 1번출구',
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
      const query = url.searchParams.get('q');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          query === '서울역' ? [origin] : query === '광교역' ? [unresolvedDestination] : [],
        ),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      geocodeQueries.push(url.searchParams.get('q') ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.goto('/');
    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);

    await page.fill('#destination', '광교역');
    await expect(page.locator('#dest-ac .ac-item')).toHaveCount(1);
    await page.locator('#dest-ac .ac-item').first().click();

    await expect(page.locator('#destination')).toHaveValue(
      '광교역(경기대)1번출구 (경기 수원시 영통구 대학로 55)',
    );
    await expect
      .poll(() => geocodeQueries, { timeout: 3000 })
      .toContain('광교역 1번출구');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
  });

  test('search waits for selected destination coords and never posts unresolved metadata', async ({
    page,
  }) => {
    const destinationGeocode = createDeferred<void>();
    const routeRequests: string[] = [];
    const tripRequests: Array<{ path: string; payload: Record<string, unknown> }> = [];
    const geocodeQueries: string[] = [];
    const origin = {
      lat: 37.554722,
      lon: 126.970833,
      display_name: '서울역',
      address: '서울 중구 한강대로 405',
      type: '역',
      source: 'naver_map',
      confidence: 0.99,
      coords_ready: true,
      selection_kind: 'station',
      canonical_query: '서울역',
    };
    const unresolvedDestination = {
      lat: null,
      lon: null,
      display_name: '광교역(경기대)1번출구',
      address: '경기 수원시 영통구 대학로 55',
      type: '출입구',
      source: 'naver_browser_suggest',
      confidence: 0.9,
      coords_ready: false,
      selection_kind: 'poi',
      canonical_query: '광교역 1번출구',
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
      const query = url.searchParams.get('q');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          query === '서울역' ? [origin] : query === '광교역' ? [unresolvedDestination] : [],
        ),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q') ?? '';
      geocodeQueries.push(query);
      if (query === '광교역 1번출구') {
        await destinationGeocode.promise;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          query === '광교역 1번출구'
            ? [
                {
                  lat: 37.300601,
                  lon: 127.044201,
                  source: 'test-geocode',
                  confidence: 0.99,
                },
              ]
            : [],
        ),
      });
    });

    await page.route('**/api/route?*', async (route) => {
      routeRequests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          routes: [
            {
              geometry: {
                type: 'LineString',
                coordinates: [
                  [126.970833, 37.554722],
                  [127.044201, 37.300601],
                ],
              },
            },
          ],
        }),
      });
    });

    await page.route('**/v1/trip/arrival-time-with-recommendation/stream', async (route) => {
      const path = new URL(route.request().url()).pathname;
      const payload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      tripRequests.push({ path, payload });
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([
          {
            event: 'arrival',
            data: {
              arrival: {
                route: { origin: payload.origin, destination: payload.destination },
                departure_time: payload.departure_time,
                arrival_time: '2026-04-27T06:10:00+09:00',
                duration_seconds: 2400,
                provider: 'naver_selenium',
                cache_hit: false,
              },
              immediate_safe_departure: null,
              progress: {
                checked: 0,
                planned: 1,
                remaining: 1,
                total_candidates: 1,
              },
            },
          },
          {
            event: 'recommendation',
            data: {
              route: { origin: payload.origin, destination: payload.destination },
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
              candidate_evaluations: [],
            },
          },
        ]),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      const path = new URL(route.request().url()).pathname;
      const payload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      tripRequests.push({ path, payload });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: { origin: payload.origin, destination: payload.destination },
          departure_time: payload.departure_time,
          arrival_time: '2026-04-27T06:10:00+09:00',
          duration_seconds: 2400,
          provider: 'naver_selenium',
          cache_hit: false,
        }),
      });
    });

    await page.goto('/');
    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();
    await page.fill('#destination', '광교역');
    await expect(page.locator('#dest-ac .ac-item')).toHaveCount(1);
    await page.locator('#dest-ac .ac-item').first().click();
    await page.click('#search-btn');

    await expect.poll(() => geocodeQueries).toContain('광교역 1번출구');
    await expect
      .poll(() => ({ routes: routeRequests.length, trips: tripRequests.length }))
      .toEqual({ routes: 0, trips: 0 });

    destinationGeocode.resolve();
    await expect
      .poll(() => routeRequests.length, {
        timeout: 5000,
      })
      .toBe(1);
    await page.waitForTimeout(500);
    for (const request of tripRequests) {
      const destPlace = request.payload.dest_place as Record<string, unknown> | undefined;
      if (!destPlace) {
        continue;
      }
      expect(destPlace).toMatchObject({
        query: '광교역(경기대)1번출구 (경기 수원시 영통구 대학로 55)',
        display_name: '광교역(경기대)1번출구',
        canonical_query: '광교역 1번출구',
        selection_kind: 'poi',
        coords_ready: true,
        lat: 37.300601,
        lon: 127.044201,
        degraded_reason: null,
      });
      expect(Number.isFinite(destPlace.lat)).toBe(true);
      expect(Number.isFinite(destPlace.lon)).toBe(true);
    }
    expect(
      tripRequests.some((request) => {
        const destPlace = request.payload.dest_place as Record<string, unknown> | undefined;
        return destPlace?.coords_ready === false;
      }),
    ).toBe(false);
  });

  test('ignores stale destination geocode after input changes', async ({
    page,
  }) => {
    const staleDestinationGeocode = createDeferred<void>();
    const geocodeQueries: string[] = [];
    const origin = {
      lat: 37.554722,
      lon: 126.970833,
      display_name: '서울역',
      address: '서울 중구 한강대로 405',
      type: '역',
      source: 'naver_map',
      confidence: 0.99,
      coords_ready: true,
      selection_kind: 'station',
      canonical_query: '서울역',
    };
    const unresolvedDestination = {
      lat: null,
      lon: null,
      display_name: '광교역(경기대)1번출구',
      address: '경기 수원시 영통구 대학로 55',
      type: '출입구',
      source: 'naver_browser_suggest',
      confidence: 0.9,
      coords_ready: false,
      selection_kind: 'poi',
      canonical_query: '광교역 1번출구',
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
      const query = url.searchParams.get('q');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          query === '서울역' ? [origin] : query === '광교역' ? [unresolvedDestination] : [],
        ),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q') ?? '';
      geocodeQueries.push(query);
      if (query === '광교역 1번출구') {
        await staleDestinationGeocode.promise;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            lat: 37.300601,
            lon: 127.044201,
            source: 'test-geocode',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.goto('/');
    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);

    await page.fill('#destination', '광교역');
    await expect(page.locator('#dest-ac .ac-item')).toHaveCount(1);
    await page.locator('#dest-ac .ac-item').first().click();
    await expect.poll(() => geocodeQueries).toContain('광교역 1번출구');

    await page.fill('#destination', '판교역');
    staleDestinationGeocode.resolve();
    await page.waitForTimeout(1000);

    await expect(page.locator('#destination')).toHaveValue('판교역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
  });

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

  test('ignores stale geocode result after another candidate is selected', async ({
    page,
  }) => {
    const staleGeocode = createDeferred<void>();
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
    const station = {
      lat: 37.554722,
      lon: 126.970833,
      display_name: '서울역',
      address: '서울 중구 한강대로 405',
      type: '역',
      source: 'naver_map',
      confidence: 0.99,
      coords_ready: true,
      selection_kind: 'station',
      canonical_query: '서울역',
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
      const query = url.searchParams.get('q');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          query === '경수대로680번길40'
            ? [unresolvedPoi]
            : query === '서울역'
              ? [station]
              : [],
        ),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      geocodeQueries.push(url.searchParams.get('q') ?? '');
      await staleGeocode.promise;
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
    await expect.poll(() => geocodeQueries, { timeout: 3000 }).toEqual(['경수대로680번길 40']);

    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac .ac-item')).toHaveCount(1);
    await page.locator('#origin-ac .ac-item').first().click();
    await expect(page.locator('#origin')).toHaveValue('서울역');
    await expect
      .poll(async () => page.locator('.leaflet-marker-icon').count(), {
        timeout: 5000,
      })
      .toBe(1);

    staleGeocode.resolve();
    await page.waitForTimeout(1000);
    await expect(page.locator('#origin')).toHaveValue('서울역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
  });

  test('ignores stale submit search after input changes during geocode', async ({
    page,
  }) => {
    const staleDestinationGeocode = createDeferred<void>();
    const geocodeQueries: string[] = [];
    const tripRequests: string[] = [];
    const routeRequests: string[] = [];

    await page.route('**/api/autocomplete**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q') ?? '';
      geocodeQueries.push(query);
      if (query === '코엑스') {
        await staleDestinationGeocode.promise;
      }
      const coords =
        query === '서울역'
          ? { lat: 37.554722, lon: 126.970833 }
          : query === '코엑스'
            ? { lat: 37.511823, lon: 127.059159 }
            : { lat: 37.394776, lon: 127.11116 };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            ...coords,
            source: 'test-geocode',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.route('**/api/route?*', async (route) => {
      routeRequests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          routes: [
            {
              geometry: {
                type: 'LineString',
                coordinates: [
                  [126.970833, 37.554722],
                  [127.059159, 37.511823],
                ],
              },
            },
          ],
        }),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      tripRequests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: {
            origin: '서울역',
            destination: '코엑스',
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
      tripRequests.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([
          {
            event: 'recommendation',
            data: {
              route: {
                origin: '서울역',
                destination: '코엑스',
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
    await page.fill('#destination', '코엑스');
    await page.click('#search-btn');

    await expect
      .poll(
        () =>
          geocodeQueries.includes('서울역') &&
          geocodeQueries.includes('코엑스'),
        { timeout: 3000 },
      )
      .toBe(true);
    await page.fill('#destination', '판교역');

    staleDestinationGeocode.resolve();
    await page.waitForTimeout(1500);

    await expect(page.locator('#destination')).toHaveValue('판교역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
    expect(routeRequests).toEqual([]);
    expect(tripRequests).toEqual([]);
  });

  test('clears stale search markers when input changes after marker render', async ({
    page,
  }) => {
    const delayedRoute = createDeferred<void>();
    const delayedBaseline = createDeferred<void>();
    const delayedStream = createDeferred<void>();
    const geocodeQueries: string[] = [];
    const tripRequests: string[] = [];
    const routeRequests: string[] = [];

    await page.route('**/api/autocomplete**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q') ?? '';
      geocodeQueries.push(query);
      const coords =
        query === '서울역'
          ? { lat: 37.554722, lon: 126.970833 }
          : query === '코엑스'
            ? { lat: 37.511823, lon: 127.059159 }
            : { lat: 37.394776, lon: 127.11116 };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            ...coords,
            source: 'test-geocode',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.route('**/api/route?*', async (route) => {
      routeRequests.push(route.request().url());
      await delayedRoute.promise;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          routes: [
            {
              geometry: {
                type: 'LineString',
                coordinates: [
                  [126.970833, 37.554722],
                  [127.059159, 37.511823],
                ],
              },
            },
          ],
        }),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      tripRequests.push(route.request().url());
      await delayedBaseline.promise;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: {
            origin: '서울역',
            destination: '코엑스',
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
      tripRequests.push(route.request().url());
      await delayedStream.promise;
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([
          {
            event: 'recommendation',
            data: {
              route: {
                origin: '서울역',
                destination: '코엑스',
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
    await page.fill('#destination', '코엑스');
    await page.click('#search-btn');

    await expect
      .poll(
        () =>
          geocodeQueries.includes('서울역') &&
          geocodeQueries.includes('코엑스'),
        { timeout: 3000 },
      )
      .toBe(true);
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(2);

    await page.fill('#destination', '판교역');
    await expect(page.locator('#destination')).toHaveValue('판교역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');

    delayedRoute.resolve();
    delayedBaseline.resolve();
    delayedStream.resolve();
    await page.waitForTimeout(1000);

    await expect(page.locator('#destination')).toHaveValue('판교역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
    await expect(page.locator('#results')).toHaveClass(/hidden/);
    expect(routeRequests.length).toBeGreaterThanOrEqual(1);
    expect(tripRequests.length).toBeGreaterThanOrEqual(1);
  });

  test('keeps new search loading visible when stale search baseline resolves', async ({
    page,
  }) => {
    const firstRoute = createDeferred<void>();
    const firstBaseline = createDeferred<void>();
    const firstStream = createDeferred<void>();
    const secondRoute = createDeferred<void>();
    const secondBaseline = createDeferred<void>();
    const secondStream = createDeferred<void>();
    const routeRequests: string[] = [];
    const tripRequests: string[] = [];

    await page.route('**/api/autocomplete**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q') ?? '';
      const coords =
        query === '서울역'
          ? { lat: 37.554722, lon: 126.970833 }
          : query === '코엑스'
            ? { lat: 37.511823, lon: 127.059159 }
            : { lat: 37.394776, lon: 127.11116 };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            ...coords,
            source: 'test-geocode',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.route('**/api/route?*', async (route) => {
      routeRequests.push(route.request().url());
      await (routeRequests.length === 1 ? firstRoute.promise : secondRoute.promise);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          routes: [
            {
              geometry: {
                type: 'LineString',
                coordinates: [
                  [126.970833, 37.554722],
                  [127.059159, 37.511823],
                ],
              },
            },
          ],
        }),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      tripRequests.push(route.request().url());
      await (tripRequests.length === 1 ? firstBaseline.promise : secondBaseline.promise);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: {
            origin: '서울역',
            destination: '코엑스',
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
      await (tripRequests.length <= 1 ? firstStream.promise : secondStream.promise);
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([
          {
            event: 'recommendation',
            data: {
              route: {
                origin: '서울역',
                destination: '코엑스',
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
    await page.fill('#destination', '코엑스');
    await page.click('#search-btn');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(2);

    await page.fill('#destination', '판교역');
    await expect(page.locator('.leaflet-marker-icon')).toHaveCount(1);
    await expect(page.locator('.leaflet-marker-icon').first()).toContainText('출발');
    await page.click('#search-btn');
    await expect(page.locator('#loading')).not.toHaveClass(/hidden/);

    firstBaseline.resolve();
    await page.waitForTimeout(500);
    await expect(page.locator('#loading')).not.toHaveClass(/hidden/);
    await expect(page.locator('#search-btn')).toBeDisabled();

    secondBaseline.resolve();
    secondRoute.resolve();
    secondStream.resolve();
    firstRoute.resolve();
    firstStream.resolve();
  });

  test('clears in-flight search state when selecting an autocomplete item', async ({
    page,
  }) => {
    const delayedBaseline = createDeferred<void>();
    const baselineRequests: string[] = [];
    const station = {
      lat: 37.511823,
      lon: 127.059159,
      display_name: '코엑스',
      address: '서울 강남구 영동대로 513',
      type: '장소',
      source: 'naver_map',
      confidence: 0.99,
      coords_ready: true,
      selection_kind: 'poi',
      canonical_query: '코엑스',
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
        body: JSON.stringify(url.searchParams.get('q') === '코엑스' ? [station] : []),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      const query = url.searchParams.get('q') ?? '';
      const coords =
        query === '서울역'
          ? { lat: 37.554722, lon: 126.970833 }
          : { lat: 37.511823, lon: 127.059159 };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            ...coords,
            source: 'test-geocode',
            confidence: 0.99,
          },
        ]),
      });
    });

    await page.route('**/api/route?*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          routes: [
            {
              geometry: {
                type: 'LineString',
                coordinates: [
                  [126.970833, 37.554722],
                  [127.059159, 37.511823],
                ],
              },
            },
          ],
        }),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      baselineRequests.push(route.request().url());
      await delayedBaseline.promise;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route: {
            origin: '서울역',
            destination: '코엑스',
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
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([
          {
            event: 'recommendation',
            data: {
              route: {
                origin: '서울역',
                destination: '코엑스',
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
              candidate_evaluations: [],
            },
          },
        ]),
      });
    });

    try {
      await page.goto('/');
      await page.click('#tab-departure');
      await page.fill('#datetime-input', futureLocalDatetime('06:30'));
      await page.fill('#origin', '서울역');
      await page.fill('#destination', '코엑스');
      await expect(page.locator('#dest-ac .ac-item')).toHaveCount(1);
      // Keep the destination autocomplete open while the in-flight search starts.
      await page.locator('#search-btn').dispatchEvent('click');
      await expect(page.locator('#loading')).not.toHaveClass(/hidden/);
      await expect
        .poll(() => baselineRequests.length, { timeout: 3000 })
        .toBeGreaterThanOrEqual(1);

      await page.locator('#destination').focus();
      await expect(page.locator('#dest-ac .ac-item')).toHaveCount(1);
      await page.locator('#dest-ac .ac-item').first().dispatchEvent('mousedown', {
        button: 0,
        bubbles: true,
        cancelable: true,
      });

      await expect(page.locator('#loading')).toHaveClass(/hidden/);
      await expect(page.locator('#search-btn')).toBeEnabled();
    } finally {
      delayedBaseline.resolve();
    }
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

  test('exposes option roles and keeps delayed lookup status non-blocking', async ({
    page,
  }, testInfo) => {
    const delayedGeocode = createDeferred<void>();
    const delayedRoute = createDeferred<void>();
    const artifactsDir = testInfo.outputPath('task-4-contract');
    mkdirSync(artifactsDir, { recursive: true });
    const origin = {
      lat: null,
      lon: null,
      display_name: '서울역 1번출구',
      address: '서울 중구 세종대로 지하 2',
      type: '출입구',
      source: 'naver_browser_suggest',
      confidence: 0.9,
      coords_ready: false,
      selection_kind: 'poi',
      canonical_query: '서울역 1번출구',
    };
    const destination = {
      lat: 37.394761,
      lon: 127.111217,
      display_name: '판교역',
      address: '경기 성남시 분당구 판교역로 지하 160',
      type: '역',
      source: 'naver_map',
      confidence: 0.98,
      coords_ready: true,
      selection_kind: 'station',
      canonical_query: '판교역',
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
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(query.includes('서울역') ? [origin] : [destination]),
      });
    });

    await page.route('**/api/geocode?*', async (route) => {
      const url = new URL(route.request().url());
      if ((url.searchParams.get('q') ?? '').includes('서울역')) {
        await delayedGeocode.promise;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([{ lat: 37.554722, lon: 126.970833 }]),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ lat: destination.lat, lon: destination.lon }]),
      });
    });

    await page.route('**/v1/trip/arrival-time', async (route) => {
      await delayedRoute.promise;
      const payload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
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
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSseBody([]),
      });
    });

    await page.goto('/');
    await expect(page.locator('text=데이터 제공자')).toHaveCount(0);
    await expect(page.locator('#provider-badge')).toHaveCount(0);

    await page.fill('#origin', '서울역');
    await expect(page.locator('#origin-ac')).toHaveAttribute('role', 'listbox');
    const originOptions = page.locator('#origin-ac [role="option"]');
    await expect(originOptions).toHaveCount(1);
    await expect(originOptions.first()).toContainText('서울역 1번출구');
    await originOptions.first().click();
    await expect(page.locator('#origin-ac')).toHaveClass(/hidden/);

    await page.fill('#destination', '판교역');
    const destOptions = page.locator('#dest-ac [role="option"]');
    await expect(destOptions).toHaveCount(1);
    await destOptions.first().click();
    await expect(page.locator('#dest-ac')).toHaveClass(/hidden/);

    await page.fill('#datetime-input', futureLocalDatetime('06:30'));
    await page.click('#search-btn');
    await expect(page.locator('#loading')).toBeVisible();

    const geometry = await page.evaluate(() => {
      const rect = (selector: string) => {
        const element = document.querySelector(selector);
        if (!element) return null;
        const box = element.getBoundingClientRect();
        return {
          left: box.left,
          top: box.top,
          right: box.right,
          bottom: box.bottom,
          width: box.width,
          height: box.height,
        };
      };
      const overlap = (
        left: ReturnType<typeof rect>,
        right: ReturnType<typeof rect>,
      ) => {
        if (!left || !right) return 0;
        const x = Math.max(0, Math.min(left.right, right.right) - Math.max(left.left, right.left));
        const y = Math.max(0, Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top));
        return x * y;
      };
      const loadingStyle = window.getComputedStyle(document.querySelector('#loading')!);
      return {
        optionRoleCount: document.querySelectorAll('[role="option"]').length,
        providerTextVisible: document.body.innerText.includes('데이터 제공자'),
        loadingPosition: loadingStyle.position,
        loadingSearchOverlap: overlap(rect('#loading'), rect('#search-btn')),
        loadingSidebarRatio:
          (rect('#loading')!.width * rect('#loading')!.height) /
          (rect('#sidebar')!.width * rect('#sidebar')!.height),
        searchButtonTop: rect('#search-btn')!.top,
        loadingBottom: rect('#loading')!.bottom,
      };
    });

    await page.screenshot({ path: join(artifactsDir, 'non-blocking-status.png'), fullPage: true });
    writeFileSync(
      join(artifactsDir, 'observables.json'),
      `${JSON.stringify(geometry, null, 2)}\n`,
      'utf-8',
    );

    expect(geometry.providerTextVisible).toBe(false);
    expect(geometry.optionRoleCount).toBeGreaterThan(0);
    expect(geometry.loadingPosition).not.toBe('absolute');
    expect(geometry.loadingSearchOverlap).toBe(0);
    expect(geometry.loadingSidebarRatio).toBeLessThan(0.25);
    expect(geometry.loadingBottom).toBeGreaterThan(geometry.searchButtonTop);

    delayedGeocode.resolve();
    delayedRoute.resolve();
    await expect(page.locator('#loading')).toHaveClass(/hidden/);
  });
});
