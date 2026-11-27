import { expect, test, type Page } from '@playwright/test';

type ViewportProbe = {
  height: number;
  offsetTop: number;
  scale: number;
  dispatchEvent: (event: Event) => void;
};

async function resizeVisualViewport(page: Page, height: number, scale = 1) {
  await page.evaluate(({ h, zoom }) => {
    const viewport = window.visualViewport as unknown as ViewportProbe;
    viewport.height = h;
    viewport.scale = zoom;
    viewport.dispatchEvent(new Event('resize'));
  }, { h: height, zoom: scale });
}

test.describe('mobile sheet', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test.beforeEach(async ({ page }) => {
    await page.route('https://unpkg.com/**', (request) => request.abort());
    await page.addInitScript(() => {
      const viewport = Object.assign(new EventTarget(), {
        height: window.innerHeight, width: window.innerWidth, offsetTop: 0, scale: 1,
      });
      Object.defineProperty(window, 'visualViewport', { value: viewport, configurable: true });
    });
    await page.goto('/');
    await expect(page.locator('#datetime-input')).not.toHaveValue('');
    await page.bringToFront();
  });

  test('shifts only while a foreground text input and keyboard overlap at normal zoom', async ({ page }) => {
    const sidebar = page.locator('#sidebar');
    const origin = page.locator('#origin');
    await resizeVisualViewport(page, 520);
    await expect(sidebar).not.toHaveClass(/kb-tracking/);

    // Full viewport with keyboard-free focus.
    await resizeVisualViewport(page, 844);
    await origin.focus();
    await expect(sidebar).not.toHaveClass(/kb-tracking/);
    await resizeVisualViewport(page, 520);
    await expect(sidebar).toHaveClass(/kb-tracking/);
    await expect(sidebar).toHaveCSS('bottom', '324px');
    const maxHeight = await sidebar.evaluate((element) => ({
      actual: (element as HTMLElement).style.maxHeight,
      expected: Math.min(element.scrollHeight, 496) + 'px',
    }));
    expect(maxHeight.actual).toBe(maxHeight.expected);

    // Pinch-zoom resizing leaves the sheet in its regular position.
    await resizeVisualViewport(page, 520, 1.3);
    await expect(sidebar).not.toHaveClass(/kb-tracking/);
    await resizeVisualViewport(page, 520);
    await expect(sidebar).toHaveClass(/kb-tracking/);

    // Blur clears the offset even if the viewport reports no further resize.
    await origin.blur();
    await expect(sidebar).not.toHaveClass(/kb-tracking/);
    await expect(sidebar).toHaveCSS('bottom', '0px');
  });

  test('reopens a dismissed sheet using its bottom button', async ({ page }) => {
    const sidebar = page.locator('#sidebar');
    const header = await page.locator('.sidebar-header').boundingBox();
    expect(header).not.toBeNull();
    const x = header!.x + header!.width / 2;
    const y = header!.y + 8;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x, y + 135, { steps: 6 });
    await page.mouse.up();
    await expect(sidebar).toHaveClass(/sheet-hidden/);
    await expect(sidebar).toHaveAttribute('inert', '');
    const restore = page.getByRole('button', { name: '검색 패널 다시 열기' });
    await expect(restore).toBeVisible();
    await restore.click();
    await expect(sidebar).not.toHaveClass(/sheet-hidden/);
    await expect(restore).toBeHidden();
    await expect(page.locator('#search-btn')).toBeVisible();
  });

  test('selects a future date on the first tap while either date input has focus', async ({ page }) => {
    await page.clock.setFixedTime(new Date('2030-06-04T06:00:00Z'));
    await page.reload();
    const nativeInput = page.locator('#datetime-input');
    await expect(nativeInput).toHaveValue('2030-06-04T15:10');
    await nativeInput.focus();
    await nativeInput.press('ArrowDown');
    await page.locator('.datetime-day-btn[data-day="5"]').tap();
    await expect(nativeInput).toHaveValue('2030-06-05T15:10');
    await expect(page.locator('.datetime-day-btn[data-day="5"]')).toHaveAttribute('aria-pressed', 'true');

    await page.locator('#datetime-day').focus();
    await resizeVisualViewport(page, 560);
    await expect(page.locator('#sidebar')).toHaveClass(/kb-tracking/);
    await page.locator('.datetime-day-btn[data-day="6"]').tap();
    await expect(nativeInput).toHaveValue('2030-06-06T15:10');
    await expect(page.locator('#sidebar')).not.toHaveClass(/kb-tracking/);
  });
});
