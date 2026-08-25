import { expect, test, type Page } from '@playwright/test';

/**
 * 크로스 디바이스/해상도 회귀 검증.
 *
 * 실행 (로컬 fixture 서버 자동 기동):
 *   TTS_PROVIDER=mock npx playwright test --config=tests/e2e/cross-device.config.ts
 *
 * 브라우저 매트릭스는 config의 projects(chromium/firefox/webkit)가 담당하고,
 * 각 브라우저에서 5개 뷰포트(모바일~FHD)를 순회한다.
 */

const VIEWPORTS = [
  { name: 'iphone-se', width: 375, height: 667, mobile: true },
  { name: 'iphone14', width: 390, height: 844, mobile: true },
  { name: 'ipad-mini', width: 768, height: 1024, mobile: false },
  { name: 'laptop-hd', width: 1366, height: 768, mobile: false },
  { name: 'desktop-fhd', width: 1920, height: 1080, mobile: false },
];

async function openHome(page: Page, viewport: (typeof VIEWPORTS)[number]) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await page.goto('/');
  await expect(page.locator('#origin')).toBeVisible();
  // ≤768px 반응형에서는 입력 UI가 오프캔버스 사이드바 안에 있다.
  if (viewport.width <= 768) {
    await page.locator('#mobile-toggle').click();
    await expect
      .poll(() => page.locator('#sidebar').getAttribute('class'))
      .toContain('open');
    await page.waitForTimeout(400);
  }
}

/** 자동완성 후보를 선택해야 프론트가 유효한 경로로 인정한다. */
async function selectPlace(
  page: Page,
  inputSelector: string,
  dropdownSelector: string,
  query: string,
) {
  const input = page.locator(inputSelector);
  await input.click();
  await input.fill('');
  await input.fill(query);
  await expect(
    page.locator(`${dropdownSelector} .ac-item`).first(),
  ).toBeVisible({ timeout: 30_000 });
  await page.locator(`${dropdownSelector} .ac-item`).first().click();
}

test.describe('cross-device rendering', () => {
  for (const viewport of VIEWPORTS) {
    test(`[${viewport.name}] 핵심 UI 렌더링 + 가로 오버플로 없음`, async ({
      page,
    }) => {
      await openHome(page, viewport);

      await expect(page.locator('#destination')).toBeVisible();
      await expect(page.locator('#search-btn')).toBeVisible();
      await expect(page.locator('#tab-arrival')).toBeVisible();
      await expect(page.locator('#tab-departure')).toBeVisible();

      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      // 1px 오차 허용
      expect(overflow.scrollWidth).toBeLessThanOrEqual(
        overflow.innerWidth + 1,
      );
    });

    test(`[${viewport.name}] mock 검색 E2E 결과 렌더링`, async ({ page }) => {
      test.setTimeout(180_000);
      await openHome(page, viewport);

      await page.locator('#tab-arrival').click();
      await selectPlace(page, '#origin', '#origin-ac', '강남역');
      await selectPlace(page, '#destination', '#dest-ac', '판교역');
      await page.locator('#search-btn').click();

      const results = page.locator('#results');
      await expect(results).toContainText(/출발|도착|추천/, {
        timeout: 150_000,
      });
      await expect(page.locator('#error-msg')).toBeHidden();
    });
  }

  test('[iphone-se] 모바일 사이드바 토글 동작', async ({ page }) => {
    // openHome을 쓰지 않는다: 닫힌 상태에서 시작해야 토글 사이클을 검증할 수 있다.
    await page.setViewportSize({
      width: VIEWPORTS[0].width,
      height: VIEWPORTS[0].height,
    });
    await page.goto('/');
    await expect(page.locator('#mobile-toggle')).toBeVisible();

    await page.locator('#mobile-toggle').click();
    await expect
      .poll(() => page.locator('#sidebar').getAttribute('class'), {
        timeout: 10_000,
      })
      .toContain('open');
    await page.waitForTimeout(400);

    await page.locator('#mobile-toggle').click();
    await expect
      .poll(() => page.locator('#sidebar').getAttribute('class'), {
        timeout: 10_000,
      })
      .not.toContain('open');
  });

  test('[desktop-fhd] 데스크톱 사이드바 상시 표시', async ({ page }) => {
    await openHome(page, VIEWPORTS[4]);
    const visible = await page.locator('#sidebar').isVisible();
    expect(visible).toBeTruthy();
  });

  test('[ipad-mini] 날짜/시간 피커 열기·선택', async ({ page }) => {
    await openHome(page, VIEWPORTS[2]);
    const input = page.locator('#datetime-input');
    const before = await input.inputValue();
    await input.click();
    const panel = page.locator('#datetime-panel');
    try {
      await expect(panel).toBeVisible({ timeout: 5_000 });
    } catch {
      await page.locator('#datetime-toggle').click();
      await expect(panel).toBeVisible({ timeout: 10_000 });
    }

    // 앱은 과거 시각 선택을 현재로 클램프하므로(정상 도메인 동작)
    // 브라우저 시각 기준 +6시간의 명확한 미래를 선택한다.
    const target = await page.evaluate(() => {
      const t = new Date(Date.now() + 6 * 60 * 60 * 1000);
      const minute = Math.floor(t.getMinutes() / 10) * 10;
      return {
        year: t.getFullYear(),
        month: t.getMonth(),
        day: t.getDate(),
        period: t.getHours() >= 12 ? 'pm' : 'am',
        hour12: String(t.getHours() % 12 || 12).padStart(2, '0'),
        minute: String(minute).padStart(2, '0'),
      };
    });

    // 시간만 먼저 바꾸면 오늘 날짜 기준 과거 조합이 하한 클램프로 되돌아가
    // 값이 고정될 수 있다. 먼저 대상 '날짜'를 선택해 미래로 만든 뒤 시간을
    // 선택한다. 달이 바뀌는 경우 다음 달로 이동한다(+6시간이라 최대 1개월).
    const view = await page.evaluate(() => {
      const title = document
        .getElementById('datetime-calendar-title')!
        .textContent!.trim();
      const m = title.match(/(\d{4})년\s*(\d{1,2})월/);
      return m ? { year: Number(m[1]), month: Number(m[2]) - 1 } : null;
    });
    if (!view || view.year !== target.year || view.month !== target.month) {
      await page.locator('#datetime-next-month').click();
    }
    await page.locator(`.datetime-day-btn[data-day="${target.day}"]`).click();

    await page.locator('#datetime-period').selectOption(target.period);
    await page.locator('#datetime-hour').selectOption(target.hour12);
    await page.locator('#datetime-minute').selectOption(target.minute);

    // 선택이 예약 시각(input)에 반영되는지 — 값 고정이 아니라 변경 자체를 검증
    await expect
      .poll(() => input.inputValue(), { timeout: 5_000 })
      .not.toBe(before);
  });
});
