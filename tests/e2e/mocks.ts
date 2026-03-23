import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import type { Page } from '@playwright/test';

type GeocodeResponse = {
  lat: string;
  lon: string;
  display_name: string;
  address: string;
  type?: string;
};

const GEOCODE_BY_QUERY: Record<string, GeocodeResponse> = {
  '강남역': {
    lat: '37.4979',
    lon: '127.0276',
    display_name: '강남역',
    address: '서울 강남구 강남대로 지하 396',
    type: '역',
  },
  '판교역': {
    lat: '37.3948',
    lon: '127.1112',
    display_name: '판교역',
    address: '경기 성남시 분당구 판교역로 160',
    type: '역',
  },
};

function resolveGeocode(query: string): GeocodeResponse[] {
  const trimmed = query.trim();
  if (trimmed in GEOCODE_BY_QUERY) {
    return [GEOCODE_BY_QUERY[trimmed]];
  }
  const fallbackKey = Object.keys(GEOCODE_BY_QUERY).find((key) => trimmed.includes(key));
  return fallbackKey ? [GEOCODE_BY_QUERY[fallbackKey]] : [];
}

export async function setupCommonMocks(page: Page): Promise<void> {
  await page.route('**/api/config', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        naver_map_client_id: null,
        timezone: 'Asia/Seoul',
        provider: 'mock',
      }),
    });
  });

  await page.route('**/api/autocomplete**', async (route) => {
    const requestUrl = new URL(route.request().url());
    const query = requestUrl.searchParams.get('q') ?? '';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(resolveGeocode(query)),
    });
  });

  await page.route('**/api/geocode**', async (route) => {
    const requestUrl = new URL(route.request().url());
    const query = requestUrl.searchParams.get('q') ?? '';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(resolveGeocode(query)),
    });
  });

  await page.route('**/api/route**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'Ok',
        routes: [
          {
            geometry: {
              type: 'LineString',
              coordinates: [
                [127.0276, 37.4979],
                [127.1112, 37.3948],
              ],
            },
          },
        ],
      }),
    });
  });
}

export async function saveCapture(page: Page, filePath: string): Promise<void> {
  mkdirSync(dirname(filePath), { recursive: true });
  await page.screenshot({ path: filePath, fullPage: true });
}
