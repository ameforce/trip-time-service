import { expect, test } from '@playwright/test';

/**
 * iOS Safari는 포커스된 폼 컨트롤의 계산(computed) 폰트 크기가 16px 미만이면
 * 페이지를 자동으로 확대한다(사용자가 다시 축소해야 하는 증상의 원인).
 * Playwright 데스크톱 WebKit 엔진은 이 동작을 재현하지 못하므로, 실제 확대
 * 발생 대신 그 선행 조건인 "모든 포커스 가능 폼 컨트롤의 computed font-size
 * >= 16px"을 모바일 뷰포트에서 검증한다.
 */
test.describe('mobile focus zoom prevention', () => {
  const FOCUSABLE_CONTROLS = [
    { selector: '#origin', label: '출발지 입력' },
    { selector: '#destination', label: '도착지 입력' },
    { selector: '#fav-name', label: '즐겨찾기 별칭 입력' },
    { selector: '#fav-address', label: '즐겨찾기 주소 입력' },
    { selector: '.datetime-input-wrap input', label: '날짜/시간 입력' },
    { selector: '.datetime-ymd-inputs input', label: '연/월/일 입력' },
    { selector: '.datetime-time-col select', label: '오전/오후·시·분 선택' },
  ];

  test('form controls keep >=16px computed font size on mobile viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await page.locator('#mobile-toggle').click();
    await page.locator('#sidebar.open').waitFor({ state: 'visible' });

    const controls = await page.evaluate((selectors) => {
      return selectors.flatMap(({ selector, label }) =>
        Array.from(document.querySelectorAll(selector)).map((element) => ({
          selector,
          label,
          id: element.id || null,
          tag: element.tagName.toLowerCase(),
          fontSize: Number.parseFloat(window.getComputedStyle(element).fontSize),
        })),
      );
    }, FOCUSABLE_CONTROLS);

    expect(controls.length).toBeGreaterThanOrEqual(FOCUSABLE_CONTROLS.length);
    for (const control of controls) {
      expect(
        control.fontSize,
        `${control.label} (${control.selector}) 의 font-size`,
      ).toBeGreaterThanOrEqual(16);
    }
  });

  test('viewport meta keeps pinch-zoom available', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    const content = await page.locator('meta[name="viewport"]').getAttribute('content');
    expect(content).toBeTruthy();
    // 자동 확대 차단을 user-scalable=no / maximum-scale=1 로 해결하면
    // 사용자 확대(WCAG 1.4.4)까지 막히므로 금지한다.
    expect(content!).not.toMatch(/user-scalable\s*=\s*no/i);
    expect(content!).not.toMatch(/maximum-scale/i);
  });
});