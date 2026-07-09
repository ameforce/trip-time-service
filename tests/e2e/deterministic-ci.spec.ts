import { expect, test } from '@playwright/test';

import { BASE_URL } from './live_utils';

const E2E_DEBUG_TOKEN = process.env.TTS_E2E_DEBUG_TOKEN ?? '';

function debugHeaders(): Record<string, string> {
  return E2E_DEBUG_TOKEN ? { 'X-TTS-Debug-Token': E2E_DEBUG_TOKEN } : {};
}

test.describe('deterministic ci lane', () => {
  test('uses fixture geo/route seams with zero external provider calls', async ({
    request,
  }) => {
    test.skip(
      process.env.TTS_E2E_FIXTURE_MODE !== '1',
      'fixture-only assertion for e2e:ci',
    );

    const warmup = await request.post(`${BASE_URL}/api/autocomplete/warmup`, {
      data: { queries: ['강남역', '서울역'], blocking: false },
    });
    expect(warmup.ok()).toBeTruthy();
    expect(await warmup.json()).toMatchObject({
      fixture_mode: true,
      external_provider_calls: 0,
    });

    const runtime = await request.get(`${BASE_URL}/api/debug/autocomplete/runtime`, {
      headers: debugHeaders(),
    });
    expect(runtime.ok()).toBeTruthy();
    const payload = await runtime.json();
    expect(payload).toMatchObject({
      fixture_mode: true,
      mode: 'fixture',
      external_provider_calls: 0,
    });
    expect(payload.external_provider_call_breakdown).toMatchObject({
      naver_all_search: 0,
      browser_autocomplete: 0,
      geocode_naver: 0,
      geocode_nominatim: 0,
      geocode_photon: 0,
      osrm_route: 0,
    });
    if (process.env.TTS_PROVIDER === 'mock') {
      expect(payload.external_provider_call_breakdown).toMatchObject({
        playwright_route_provider: 0,
      });
    }

    const route = await request.get(
      `${BASE_URL}/api/route?olat=37.1&olon=127.1&dlat=37.2&dlon=127.2`,
    );
    expect(route.ok()).toBeTruthy();
    expect(await route.json()).toMatchObject({
      routes: [
        {
          source: 'e2e_fixture',
          geometry: {
            type: 'LineString',
            coordinates: [
              [127.1, 37.1],
              [127.2, 37.2],
            ],
          },
        },
      ],
    });
  });
});
