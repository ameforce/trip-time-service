import { expect, test } from '@playwright/test';

/**
 * 모바일 키보드 회피 검증.
 *
 * 실기기 키보드는 데스크톱 엔진에서 재현되지 않으므로 window.visualViewport를
 * 가짜 객체로 대체하고 resize 이벤트를 직접 발화해, 키보드 겹침량만큼 시트
 * bottom 이 올라갔다가 복원되는지 검증한다(iOS 시나리오).
 */
test.describe('mobile keyboard avoidance', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      const listeners: Record<string, ((ev: unknown) => void)[]> = {};
      const fake = {
        height: 0,
        offsetTop: 0,
        width: 0,
        pageLeft: 0,
        pageTop: 0,
        scale: 1,
        addEventListener(type: string, fn: (ev: unknown) => void) {
          (listeners[type] ??= []).push(fn);
        },
        removeEventListener(type: string, fn: (ev: unknown) => void) {
          listeners[type] = (listeners[type] ?? []).filter((f) => f !== fn);
        },
        dispatch(type: string) {
          for (const fn of listeners[type] ?? []) fn.call(fake, { type });
        },
        setKeyboard(height: number | null) {
          this.height = height ?? window.innerHeight;
          this.dispatch('resize');
        },
      };
      fake.height = window.innerHeight;
      fake.width = window.innerWidth;
      Object.defineProperty(window, 'visualViewport', {
        value: fake,
        configurable: true,
      });
      (window as unknown as Record<string, unknown>).__fakeVV = fake;
    });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await page.locator('#mobile-toggle').click();
    await page.locator('#sidebar.open').waitFor({ state: 'visible' });
  });

  test('raises sheet by keyboard overlap and restores on close', async ({ page }) => {
    const bottomStyle = () =>
      page.evaluate(
        () => (document.getElementById('sidebar') as HTMLElement).style.bottom,
      );

    // 초기 상태: 키보드 없음 → 인라인 보정 없음
    expect(await bottomStyle()).toBe('');

    // 키보드 등장(가시 높이 300px 가정): overlap = 844 - 300 = 544
    await page.evaluate(() => {
      (window as unknown as { __fakeVV: { setKeyboard: (h: number) => void } })
        .__fakeVV.setKeyboard(300);
    });
    await expect.poll(bottomStyle).toBe('544px');

    const maxHeight = await page.evaluate(
      () => (document.getElementById('sidebar') as HTMLElement).style.maxHeight,
    );
    // 브라우저가 calc(300px - 12px)을 calc(288px)로 정규화한다
    expect(maxHeight).toBe('calc(288px)');

    // 키보드 접힘: 인라인 보정 제거(CSS 클래스 값으로 복원)
    await page.evaluate(() => {
      (
        window as unknown as {
          __fakeVV: { setKeyboard: (h: number | null) => void };
        }
      ).__fakeVV.setKeyboard(null);
    });
    await expect.poll(bottomStyle).toBe('');
    const restoredMaxHeight = await page.evaluate(
      () => (document.getElementById('sidebar') as HTMLElement).style.maxHeight,
    );
    expect(restoredMaxHeight).toBe('');
  });

  test('focused input scrolls into view inside sheet', async ({ page }) => {
    await page.locator('#destination').click();
    // focusin 핸들러(150ms 지연) 이후 입력이 시트 가시 영역 안에 있어야 한다
    await page.waitForTimeout(600);

    const visible = await page.evaluate(() => {
      const sheet = document.getElementById('sidebar')!;
      const input = document.getElementById('destination')!;
      const sheetRect = sheet.getBoundingClientRect();
      const inputRect = input.getBoundingClientRect();
      return (
        inputRect.top >= sheetRect.top && inputRect.bottom <= sheetRect.bottom
      );
    });
    expect(visible).toBeTruthy();
  });
});
