import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type Request as PlaywrightRequest,
} from '@playwright/test';

import {
  assertStrictRoutePlacePayload,
  clearAutocompleteCache,
  saveCapture,
  verifyAutocompleteApi,
  writeJsonArtifact,
} from './live_utils';

type SelectionCase = {
  inputSelector: '#origin' | '#destination';
  dropdownSelector: '#origin-ac' | '#dest-ac';
  query: string;
  selectedExact?: string;
  selectedContains?: string;
  matcher?: (text: string) => boolean;
};

type RoadAddressScenario = {
  name: string;
  slug: string;
  roadAddressQuery: string;
  canonicalRoadQuery: string;
  verboseRoadTokens: string[];
  origin: SelectionCase;
  destination: SelectionCase;
  canonicalRoadIn: 'origin' | 'destination';
  otherSideTokens: string[];
};

const ROAD_ADDRESS_EXPECTED_ANY = [
  '경수대로680번길40',
  '경수대로680번길',
  '40',
  '센트럴하우스',
];

const ROAD_ADDRESS_SCENARIOS: RoadAddressScenario[] = [
  {
    name: '경수대로680번길40 -> 잠실역',
    slug: 'road-address-to-jamsil',
    roadAddressQuery: '경수대로680번길40',
    canonicalRoadQuery: '경수대로680번길 40',
    verboseRoadTokens: ['센트럴하우스'],
    origin: {
      inputSelector: '#origin',
      dropdownSelector: '#origin-ac',
      query: '경수대로680번길40',
      selectedExact: '경수대로680번길 40',
    },
    destination: {
      inputSelector: '#destination',
      dropdownSelector: '#dest-ac',
      query: '잠실역',
      selectedContains: '잠실역',
      matcher: (text) => text.includes('잠실역'),
    },
    canonicalRoadIn: 'origin',
    otherSideTokens: ['잠실'],
  },
  {
    name: '경수대로680번길40 -> 네이버 1784',
    slug: 'road-address-to-naver-1784',
    roadAddressQuery: '경수대로680번길40',
    canonicalRoadQuery: '경수대로680번길 40',
    verboseRoadTokens: ['센트럴하우스'],
    origin: {
      inputSelector: '#origin',
      dropdownSelector: '#origin-ac',
      query: '경수대로680번길40',
      selectedExact: '경수대로680번길 40',
    },
    destination: {
      inputSelector: '#destination',
      dropdownSelector: '#dest-ac',
      query: '네이버 1784',
      selectedContains: '네이버',
      matcher: (text) => text.includes('네이버') || text.includes('1784'),
    },
    canonicalRoadIn: 'origin',
    otherSideTokens: ['네이버', '1784'],
  },
  {
    name: '잠실역 -> 경수대로680번길40',
    slug: 'jamsil-to-road-address',
    roadAddressQuery: '경수대로680번길40',
    canonicalRoadQuery: '경수대로680번길 40',
    verboseRoadTokens: ['센트럴하우스'],
    origin: {
      inputSelector: '#origin',
      dropdownSelector: '#origin-ac',
      query: '잠실역',
      selectedContains: '잠실역',
      matcher: (text) => text.includes('잠실역'),
    },
    destination: {
      inputSelector: '#destination',
      dropdownSelector: '#dest-ac',
      query: '경수대로680번길40',
      selectedExact: '경수대로680번길 40',
    },
    canonicalRoadIn: 'destination',
    otherSideTokens: ['잠실'],
  },
];

function formatFutureLocalDatetime(offsetDays: number, hhmm: string): string {
  const [hoursText, minutesText] = hhmm.split(':');
  const current = new Date();
  current.setSeconds(0, 0);
  current.setDate(current.getDate() + offsetDays);
  current.setHours(Number(hoursText), Number(minutesText), 0, 0);
  const yyyy = String(current.getFullYear());
  const mm = String(current.getMonth() + 1).padStart(2, '0');
  const dd = String(current.getDate()).padStart(2, '0');
  const hh = String(current.getHours()).padStart(2, '0');
  const mi = String(current.getMinutes()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}T${hh}:${mi}`;
}

async function selectAutocomplete(
  page: Page,
  request: APIRequestContext,
  selection: SelectionCase,
): Promise<{ texts: string[]; selectedValue: string }> {
  const input = page.locator(selection.inputSelector);
  const items = page.locator(`${selection.dropdownSelector} .ac-item`);
  const visibilityTimeoutMs = /\d/.test(selection.query) ? 45000 : 20000;
  let lastError: unknown = null;

  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await input.click();
      await input.fill('');
      await page.waitForTimeout(150);
      await input.fill(selection.query);

      await expect(items.first()).toBeVisible({ timeout: visibilityTimeoutMs });
      const texts = (await items.allTextContents()).map((value) => value.trim());
      expect(
        texts.length,
        `autocomplete items missing for ${selection.query}`,
      ).toBeGreaterThan(0);

      const selectedIndex = selection.matcher ? texts.findIndex(selection.matcher) : 0;
      expect(
        selectedIndex,
        `autocomplete item mismatch for ${selection.query}: ${texts.join(' | ')}`,
      ).toBeGreaterThanOrEqual(0);
      await items.nth(selectedIndex).click();

      return {
        texts,
        selectedValue: await input.inputValue(),
      };
    } catch (error) {
      lastError = error;
      if (attempt >= 1) {
        break;
      }
      await clearAutocompleteCache(request);
      await page.waitForTimeout(400);
    }
  }

  throw lastError instanceof Error
    ? lastError
    : new Error(`autocomplete selection failed for ${selection.query}`);
}

function assertSelectionValue(
  selection: SelectionCase,
  selectedValue: string,
): void {
  if (selection.selectedExact) {
    expect(selectedValue).toBe(selection.selectedExact);
  }
  if (selection.selectedContains) {
    expect(selectedValue).toContain(selection.selectedContains);
  }
}

function assertCanonicalRoadPayload(
  values: string[],
  scenario: RoadAddressScenario,
  label: string,
): void {
  expect(values, `${label} should receive canonical road query`).toContain(
    scenario.canonicalRoadQuery,
  );
  for (const token of scenario.verboseRoadTokens) {
    expect(
      values.some((value) => value.includes(token)),
      `${label} should not keep verbose road token ${token}: ${values.join(' | ')}`,
    ).toBe(false);
  }
}

function assertContainsAnyToken(
  values: string[],
  tokens: string[],
  label: string,
): void {
  expect(
    values.some((value) => tokens.some((token) => value.includes(token))),
    `${label} should preserve one of ${tokens.join(', ')}: ${values.join(' | ')}`,
  ).toBe(true);
}

test.describe('road-address route regression', () => {
  for (const scenario of ROAD_ADDRESS_SCENARIOS) {
    test(scenario.name, async ({ page, request }, testInfo) => {
      await clearAutocompleteCache(request);

      const geocodeRequests: string[] = [];
      const tripOrigins: string[] = [];
      const tripDestinations: string[] = [];
      const tripPayloads: Record<string, unknown>[] = [];
      const onRequest = (pendingRequest: PlaywrightRequest): void => {
        const requestUrl = pendingRequest.url();
        if (requestUrl.includes('/api/geocode?')) {
          geocodeRequests.push(requestUrl);
        }
        if (requestUrl.includes('/v1/trip/')) {
          try {
            const payload = pendingRequest.postDataJSON() as Record<string, unknown>;
            tripPayloads.push(payload);
            tripOrigins.push(String(payload.origin ?? ''));
            tripDestinations.push(String(payload.destination ?? ''));
          } catch {
            // ignore malformed bodies during verification
          }
        }
      };
      page.on('request', onRequest);

      try {
        await page.goto('/');
        await expect(page.locator('#search-btn')).toBeVisible();
        await saveCapture(page, testInfo.outputPath(`${scenario.slug}-initial.png`));

        await verifyAutocompleteApi(
          request,
          {
            query: scenario.roadAddressQuery,
            expected_any: ROAD_ADDRESS_EXPECTED_ANY,
            min_results: 1,
            require_coords: false,
            retry_once_on_empty: true,
            category: 'road-address-route',
          },
          {
            clearCacheOnRetry: false,
            cacheClearFallbackOnFailure: true,
          },
        );

        const originSelection = await selectAutocomplete(page, request, scenario.origin);
        assertSelectionValue(scenario.origin, originSelection.selectedValue);

        const destinationSelection = await selectAutocomplete(
          page,
          request,
          scenario.destination,
        );
        assertSelectionValue(scenario.destination, destinationSelection.selectedValue);

        await page.fill('#datetime-input', formatFutureLocalDatetime(1, '19:00'));
        await saveCapture(page, testInfo.outputPath(`${scenario.slug}-ready.png`));
        await page.click('#search-btn');

        const recommendationCard = page.locator('.recommendation-card').last();
        await expect(recommendationCard).toBeVisible({ timeout: 180000 });
        await expect(recommendationCard.locator('.duration-big')).toBeVisible({
          timeout: 120000,
        });

        expect(
          geocodeRequests,
          'selected stable payload should skip frontend geocode',
        ).toHaveLength(0);
        expect(tripPayloads.length, 'trip API requests should be captured').toBeGreaterThan(0);
        for (const payload of tripPayloads) {
          assertStrictRoutePlacePayload(payload, 'origin');
          assertStrictRoutePlacePayload(payload, 'dest');
        }

        if (scenario.canonicalRoadIn === 'origin') {
          assertCanonicalRoadPayload(tripOrigins, scenario, 'trip API origin');
          assertContainsAnyToken(
            tripDestinations,
            scenario.otherSideTokens,
            'trip API destination',
          );
        } else {
          assertContainsAnyToken(
            tripOrigins,
            scenario.otherSideTokens,
            'trip API origin',
          );
          assertCanonicalRoadPayload(
            tripDestinations,
            scenario,
            'trip API destination',
          );
        }

        await saveCapture(page, testInfo.outputPath(`${scenario.slug}-final.png`));
        writeJsonArtifact(testInfo.outputPath(`${scenario.slug}-report.json`), {
          suite: 'road-address-route',
          scenario: scenario.name,
          origin_dropdown: originSelection.texts,
          destination_dropdown: destinationSelection.texts,
          trip_origins: tripOrigins,
          trip_destinations: tripDestinations,
          geocode_request_count: geocodeRequests.length,
        });
      } finally {
        page.off('request', onRequest);
      }
    });
  }
});
