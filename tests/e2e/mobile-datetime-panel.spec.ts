import { expect, test } from '@playwright/test';

test.describe('mobile datetime panel (v0.3.1 regression)', () => {
  test('panel stays fully in viewport when sheet is internally scrolled', async ({ page }) => {
    await page.setViewportSize({ width: 740, height: 360 });
    await page.goto('/');
    await page.waitForSelector('#datetime-toggle', { state: 'visible' });

    const scrollable = await page.evaluate(() => {
      const sb = document.querySelector('#sidebar')!;
      return sb.scrollHeight - sb.clientHeight;
    });
    expect(scrollable).toBeGreaterThan(0);

    await page.evaluate(() => {
      const sb = document.querySelector('#sidebar')!;
      sb.scrollTop = sb.scrollHeight;
    });
    await page.waitForTimeout(400);

    await page.click('#datetime-toggle');
    await page.waitForSelector('#datetime-panel:not(.hidden)');
    await page.waitForTimeout(400);

    const rect = await page.evaluate(() => {
      const p = document.querySelector('#datetime-panel')!;
      const r = p.getBoundingClientRect();
      return {
        parentIsBody: p.parentElement === document.body,
        top: r.top,
        left: r.left,
        right: r.right,
        bottom: r.bottom,
        vw: window.innerWidth,
        vh: window.innerHeight,
      };
    });

    expect(rect.parentIsBody).toBe(true);
    expect(rect.top).toBeGreaterThanOrEqual(0);
    expect(rect.left).toBeGreaterThanOrEqual(0);
    expect(rect.right).toBeLessThanOrEqual(rect.vw);
    expect(rect.bottom).toBeLessThanOrEqual(rect.vh);
  });

  test('backdrop tap collapses expanded sheet and datetime panel', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    // expand sheet via toggle, open datetime panel
    await page.click('#mobile-toggle');
    await page.waitForSelector('#sidebar.open', { state: 'attached' });
    await expect(page.locator('#sidebar-backdrop.is-visible')).toBeVisible();

    await page.click('#datetime-toggle');
    await page.waitForSelector('#datetime-panel:not(.hidden)');

    // exposed map area above the docked sheet belongs to the backdrop
    await page.click('#sidebar-backdrop', { position: { x: 195, y: 30 } });
    await page.waitForSelector('#sidebar.open', { state: 'detached' });
    await expect(page.locator('#sidebar-backdrop.is-visible')).toHaveCount(0);
    await expect(page.locator('#datetime-panel')).toHaveClass(/hidden/);
    await expect(page.locator('#mobile-toggle')).toHaveAttribute('aria-expanded', 'false');
  });

  test('sheet is docked and search controls visible without any interaction', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    const layout = await page.evaluate(() => {
      const q = (s: string) => document.querySelector(s)!;
      const inViewport = (s: string) => {
        const el = q(s);
        if (!el) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return (
          r.width > 0 && r.height > 0 && r.bottom <= innerHeight && r.right <= innerWidth && r.top >= 0
        );
      };
      const sr = q('#sidebar').getBoundingClientRect();
      return {
        dockedBottom: Math.abs(sr.bottom - innerHeight) < 1,
        fullWidth: Math.abs(sr.width - innerWidth) < 1,
        searchVisible: inViewport('#search-btn'),
        originVisible: inViewport('#origin'),
        destinationVisible: inViewport('#destination'),
        modeTabsVisible: inViewport('.mode-tabs'),
        zoomHidden: getComputedStyle(document.querySelector('.leaflet-control-zoom')!).display === 'none',
      };
    });
    expect(layout.dockedBottom).toBe(true);
    expect(layout.fullWidth).toBe(true);
    expect(layout.searchVisible).toBe(true);
    expect(layout.originVisible).toBe(true);
    expect(layout.destinationVisible).toBe(true);
    expect(layout.modeTabsVisible).toBe(true);
    expect(layout.zoomHidden).toBe(true);

    // collapsed by default: not .open class
    expect(await page.locator('#sidebar.open').count()).toBe(0);
  });

  test('search auto-expands the sheet on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await page.fill('#origin', '강남역');
    await page.fill('#destination', '판교역');
    await page.click('#search-btn');

    await page.waitForSelector('#sidebar.open', { state: 'attached' });
    const cardLoc = page
      .locator('.recommendation-card, .result-card, .baseline-card')
      .first();
    await expect(cardLoc).toBeVisible({ timeout: 60_000 });
    await cardLoc.scrollIntoViewIfNeeded();
    await expect(cardLoc).toBeInViewport({ ratio: 0.3 });
  });

  test('panel is clamped within short landscape viewport and scrollable', async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 360 });
    await page.goto('/');

    await page.click('#datetime-toggle');
    await page.waitForSelector('#datetime-panel:not(.hidden)');
    await page.waitForTimeout(400);

    const rect = await page.evaluate(() => {
      const p = document.querySelector('#datetime-panel')!;
      const r = p.getBoundingClientRect();
      const cs = getComputedStyle(p);
      return {
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        overflowY: cs.overflowY,
        maxH: parseInt(cs.maxHeight, 10),
        vh: window.innerHeight,
      };
    });

    expect(rect.top).toBeGreaterThanOrEqual(0);
    expect(rect.bottom).toBeLessThanOrEqual(rect.vh);
    expect(rect.overflowY).toBe('auto');
    expect(rect.maxH).toBe(rect.vh - 16);
  });

  test('panel interaction still works after portal move (desktop)', async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.goto('/');

    await page.click('#datetime-toggle');
    await page.waitForSelector('#datetime-panel:not(.hidden)');
    await page.selectOption('#datetime-hour', '08');
    await page.selectOption('#datetime-minute', '30');

    await page.mouse.click(900, 300);
    await expect(page.locator('#datetime-panel')).toHaveClass(/hidden/);

    const rect = await page.evaluate(() => {
      const r = document.querySelector('#datetime-panel')!.getBoundingClientRect();
      return { top: Math.round(r.top), left: Math.round(r.left), right: Math.round(r.right), vw: window.innerWidth };
    });
    expect(rect.left).toBeGreaterThanOrEqual(0);
    expect(rect.right).toBeLessThanOrEqual(rect.vw);
    expect(rect.top).toBeGreaterThanOrEqual(0);
  });
});
