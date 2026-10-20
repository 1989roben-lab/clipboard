/* Run with Playwright installed: node tests/todo_browser_test.cjs.
 * PLAYWRIGHT_MODULE can point to an existing installation. No live data is used. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const net = require('node:net');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'memory-todo-browser-'));
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn('python3', ['server.py'], { cwd: path.resolve(__dirname, '..'),
    env: { ...process.env, HOST: '127.0.0.1', PORT: String(port), DB_PATH: path.join(dir, 'test.db') }, stdio: 'ignore' });
  let browser;
  try {
    for (let i = 0; i < 60; i++) {
      try { if ((await fetch(base + '/health')).ok) break; } catch {}
      await sleep(100);
    }
    const api = async (url, method = 'GET', body) => {
      const response = await fetch(base + url, { method, headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body) });
      assert(response.ok); return response.json();
    };
    const entry = (await api('/api/entries', 'POST', { type: 'todo', items: ['Alpha', 'Beta', 'Gamma'] })).entry;
    const ids = entry.items.map(item => item.id);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 900, height: 750 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    const row = id => page.locator(`.todo-item[data-item-id="${id}"]`);
    const input = page.locator('.todo-item-text[contenteditable]');
    const waitClosed = () => page.waitForFunction(() => !document.querySelector('.todo-item-text[contenteditable]'));
    const appearance = locator => locator.evaluate(node => {
      const style = getComputedStyle(node), rect = node.getBoundingClientRect();
      return { font: style.fontFamily, size: style.fontSize, color: style.color, lineHeight: style.lineHeight,
        background: style.backgroundColor, border: style.border, padding: style.padding,
        width: rect.width, height: rect.height };
    });
    const todoAppearance = await appearance(row(ids[0]).locator('.todo-item-text'));
    await row(ids[0]).locator('.todo-item-text').click();
    assert.deepEqual(await appearance(input), todoAppearance);
    assert.equal(await input.evaluate(node => node.tagName), 'P');
    assert.equal(await input.evaluate(node => getComputedStyle(node).outlineStyle), 'none');
    await input.fill('Edited');
    await input.press('Enter'); await waitClosed();
    assert.equal((await api('/api/entries')).entries.find(e => e.id === entry.id).items[0].content, 'Edited');
    await row(ids[0]).locator('.todo-item-text').click(); await input.fill('cancelled'); await input.press('Escape'); await waitClosed();
    assert.equal(await row(ids[0]).locator('.todo-item-text').textContent(), 'Edited');
    await row(ids[0]).locator('.todo-item-text').click(); await input.fill('   '); await input.press('Enter');
    assert.match(await row(ids[0]).locator('.todo-inline-error').textContent(), /不能为空/);
    await input.fill('中文输入');
    await input.dispatchEvent('compositionstart');
    await input.press('Enter'); assert.equal(await input.count(), 1);
    await input.dispatchEvent('compositionend');
    await page.locator('#entry-count').click(); await waitClosed();
    assert.equal((await api('/api/entries')).entries.find(e => e.id === entry.id).items[0].content, '中文输入');
    // Failure leaves the draft editable; retry saves it.
    await page.route('**/api/todo-items/*', route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"测试保存失败"}' }));
    await row(ids[0]).locator('.todo-item-text').click(); await input.fill('Retry draft'); await input.press('Enter');
    await page.waitForFunction(() => document.querySelector('.todo-inline-error')?.textContent.includes('测试保存失败'));
    assert.equal(await input.innerText(), 'Retry draft');
    await page.unroute('**/api/todo-items/*'); await input.press('Enter'); await waitClosed();
    // A GET begun before editing must not replace the active textarea.
    let release, entered;
    const held = new Promise(resolve => { release = resolve; });
    const intercepted = new Promise(resolve => { entered = resolve; });
    await page.route('**/api/entries', async route => {
      const response = await route.fetch(); entered(); await held; await route.fulfill({ response });
    });
    await page.evaluate(() => { void loadEntries({ silent: true, force: true }); });
    await intercepted;
    await row(ids[0]).locator('.todo-item-text').click(); await input.fill('Unsaved race'); release(); await sleep(150);
    assert.equal(await input.innerText(), 'Unsaved race');
    await page.unroute('**/api/entries'); await input.press('Escape'); await waitClosed();
    // Normal text uses the original PRE, including font, geometry, newlines and paste.
    const note = (await api('/api/entries', 'POST', { content: 'First line\nSecond line' })).entry;
    await page.evaluate(() => loadEntries({ force: true }));
    const noteBody = page.locator(`pre[data-entry-id="${note.id}"]`);
    const editableNote = page.locator('pre[contenteditable]');
    const noteAppearance = await appearance(noteBody);
    await noteBody.click();
    assert.deepEqual(await appearance(editableNote), noteAppearance);
    assert.equal(await page.locator('.entry-editor').count(), 0);
    await editableNote.fill('Line one');
    await editableNote.press('End'); await editableNote.press('Enter');
    await page.keyboard.insertText('第二行');
    await page.locator('#entry-count').click();
    await page.waitForFunction(() => !inlineTodoEdit && !pendingTodoWrites);
    assert.equal((await api('/api/entries')).entries.find(e => e.id === note.id).content, 'Line one\n第二行');
    await page.locator('article').filter({ has: noteBody }).getByRole('button', { name: '编辑', exact: true }).click();
    await editableNote.fill('cancel me'); await editableNote.press('Escape');
    assert.equal(await noteBody.innerText(), 'Line one\n第二行');
    // Blank or oversized drafts prevent switching to another editor.
    await noteBody.click(); await editableNote.fill(' '); await editableNote.press('Control+Enter');
    await row(ids[0]).locator('.todo-item-text').click();
    assert.equal(await editableNote.count(), 1); assert.equal(await input.count(), 0);
    await editableNote.evaluate(node => { node.textContent = 'x'.repeat(204801); });
    await editableNote.press('Control+Enter');
    assert.match(await page.locator('.todo-inline-error').textContent(), /200 KB/);
    await editableNote.fill('Paste:'); await editableNote.press('End');
    // The explicit plain-text paste handler also supports the fallback editing mode.
    await editableNote.evaluate(node => {
      node.contentEditable = 'true';
      const transfer = new DataTransfer();
      transfer.setData('text/html', '<b>unsafe formatting</b>');
      transfer.setData('text/plain', '<b>literal</b>\nnext');
      node.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: transfer }));
    });
    await editableNote.press('Control+Enter');
    await page.waitForFunction(() => !inlineTodoEdit && !pendingTodoWrites);
    assert.equal((await api('/api/entries')).entries.find(e => e.id === note.id).content, 'Paste:<b>literal</b>\nnext');
    assert.equal(await noteBody.locator('b').count(), 0);
    // Failed normal-text saves retain the original node and draft for retry.
    await page.route(`**/api/entries/${note.id}`, route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"文字保存失败"}' }));
    await noteBody.click(); await editableNote.fill('Recovered note'); await editableNote.press('Control+Enter');
    await page.waitForFunction(() => document.querySelector('.todo-inline-error')?.textContent.includes('文字保存失败'));
    assert.equal(await editableNote.innerText(), 'Recovered note');
    // A forced background reload must preserve this failed draft.
    await page.evaluate(() => loadEntries({ force: true }));
    assert.equal(await editableNote.innerText(), 'Recovered note');
    await page.unroute(`**/api/entries/${note.id}`);
    // Switching editors awaits the previous save and edits only the requested node.
    await row(ids[0]).locator('.todo-item-text').click();
    await page.waitForFunction(() => document.querySelector('.todo-item-text[contenteditable]'));
    assert.equal(await editableNote.count(), 0);
    assert.equal((await api('/api/entries')).entries.find(e => e.id === note.id).content, 'Recovered note');
    await input.press('Escape');
    await page.reload();
    assert.equal(await noteBody.innerText(), 'Recovered note');
    // Both the metadata timestamp and normal card ordering have been refreshed.
    const savedNote = (await api('/api/entries')).entries.find(e => e.id === note.id);
    assert.notEqual(savedNote.updated_at, note.updated_at);
    // Stage buttons do not enter editing, and preserve the new content.
    await row(ids[0]).locator('[data-stage="5"]').click();
    await page.waitForFunction(() => document.querySelector('.todo-item')?.classList.contains('completed'));
    assert.equal(await input.count(), 0);
    // Keyboard reordering and mouse dragging persist without moving the card timestamp.
    await row(ids[0]).focus(); await row(ids[0]).press('Alt+ArrowDown');
    await page.waitForFunction(() => !pendingTodoWrites);
    assert.deepEqual((await api('/api/entries')).entries.find(e => e.id === entry.id).items.map(i => i.id), [ids[1], ids[0], ids[2]]);
    await row(ids[2]).scrollIntoViewIfNeeded();
    let source = await row(ids[2]).boundingBox(), target = await row(ids[1]).boundingBox();
    await page.mouse.move(source.x + 12, source.y + source.height / 2); await page.mouse.down();
    await page.mouse.move(target.x + 12, target.y + 2, { steps: 12 });
    await page.mouse.up();
    await page.waitForFunction(() => !pendingTodoWrites);
    assert.deepEqual((await api('/api/entries')).entries.find(e => e.id === entry.id).items.map(i => i.id), [ids[2], ids[1], ids[0]]);
    assert.equal(await input.count(), 0);
    assert.equal((await api('/api/entries')).entries.find(e => e.id === entry.id).updated_at, entry.updated_at);
    await page.reload(); assert.equal(await page.locator('.todo-item').first().getAttribute('data-item-id'), String(ids[2]));
    // Failed ordering restores the DOM and leaves server ordering intact.
    await page.route('**/todo-order', route => route.fulfill({ status: 409, contentType: 'application/json', body: '{"error":"清单已变化"}' }));
    await row(ids[2]).focus(); await row(ids[2]).press('Alt+ArrowDown');
    await page.waitForFunction(() => !pendingTodoWrites);
    assert.equal(await page.locator('.todo-item').first().getAttribute('data-item-id'), String(ids[2]));
    await page.unroute('**/todo-order');
    // Real Chromium touch events exercise long hold and normal scrolling.
    const mobile = await browser.newContext({ viewport: { width: 390, height: 700 }, isMobile: true, hasTouch: true });
    const phone = await mobile.newPage(); phone.on('pageerror', e => errors.push(e.message)); await phone.goto(base);
    await phone.locator('.todo-item').first().scrollIntoViewIfNeeded();
    await phone.locator('.todo-item-text').first().tap();
    await phone.locator('.todo-item-text[contenteditable]').fill('Mobile edit');
    await phone.locator('.todo-item-text[contenteditable]').press('Enter');
    await phone.waitForFunction(() => !inlineTodoEdit && !pendingTodoWrites);
    await phone.locator('.todo-item').first().scrollIntoViewIfNeeded();
    const cdp = await mobile.newCDPSession(phone);
    async function touch(type, x, y) {
      await cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' || type === 'touchCancel' ? [] : [{ x, y }] });
    }
    source = await phone.locator('.todo-item').first().boundingBox();
    target = await phone.locator('.todo-item').last().boundingBox();
    await touch('touchStart', source.x + 12, source.y + source.height / 2); await sleep(500);
    assert.equal(await phone.locator('.todo-page-ghost').count(), 1);
    await touch('touchMove', target.x + 12, target.y + target.height - 1); await touch('touchEnd');
    await phone.waitForFunction(() => !pendingTodoWrites && !pageTodoDrag);
    assert.deepEqual((await api('/api/entries')).entries.find(e => e.id === entry.id).items.map(i => i.id), [ids[1], ids[0], ids[2]]);
    assert.equal(await phone.locator('.todo-item-text[contenteditable]').count(), 0);
    // Cancelling a long hold restores order.
    source = await phone.locator('.todo-item').first().boundingBox();
    await touch('touchStart', source.x + 12, source.y + 10); await sleep(500);
    await touch('touchMove', source.x + 12, source.y + 60); await touch('touchCancel');
    assert.equal(await phone.locator('.todo-page-ghost').count(), 0);
    assert.deepEqual((await api('/api/entries')).entries.find(e => e.id === entry.id).items.map(i => i.id), [ids[1], ids[0], ids[2]]);
    // Moving before the hold threshold cancels the sort and scrolls the page.
    await phone.evaluate(() => { document.body.style.paddingBottom = '1600px'; });
    await phone.locator('.todo-item').first().scrollIntoViewIfNeeded();
    source = await phone.locator('.todo-item').first().boundingBox();
    const scrollBefore = await phone.evaluate(() => scrollY);
    await touch('touchStart', source.x + 15, source.y + 15);
    await touch('touchMove', source.x + 15, source.y - 55); await sleep(100); await touch('touchEnd');
    assert.equal(await phone.locator('.todo-page-ghost').count(), 0);
    assert(await phone.evaluate(() => scrollY) > scrollBefore);
    // Holding at the bottom edge scrolls; cancellation still restores ordering.
    await phone.locator('.todo-item').first().scrollIntoViewIfNeeded();
    source = await phone.locator('.todo-item').first().boundingBox();
    const edgeScrollBefore = await phone.evaluate(() => scrollY);
    await touch('touchStart', source.x + 12, source.y + 10); await sleep(500);
    await touch('touchMove', source.x + 12, 685); await sleep(200);
    assert(await phone.evaluate(() => scrollY) > edgeScrollBefore);
    await touch('touchCancel');
    assert.deepEqual((await api('/api/entries')).entries.find(e => e.id === entry.id).items.map(i => i.id), [ids[1], ids[0], ids[2]]);
    // The existing bulk editor still opens and closes without replacing page editing.
    await phone.locator('article').filter({ has: phone.locator('.todo-content') }).getByRole('button', { name: '编辑', exact: true }).click();
    assert.equal(await phone.locator('#todo-edit-list textarea').count(), 3);
    await phone.locator('#cancel-todo-edit').click();
    // Long bulk-edit lists must scroll independently while Save stays visible,
    // including short viewports representing the space above a mobile keyboard.
    const longTodo = (await api('/api/entries', 'POST', { type: 'todo',
      items: Array.from({ length: 35 }, (_, index) => `项目 ${index + 1}：很长的待办内容，需要多行显示并可以继续添加事项。`) })).entry;
    await phone.evaluate(() => loadEntries({ force: true }));
    await phone.locator('article').filter({ has: phone.locator(`.todo-item[data-item-id="${longTodo.items[0].id}"]`) })
      .getByRole('button', { name: '编辑', exact: true }).click();
    async function checkModalFits() {
      const metrics = await phone.evaluate(() => {
        const list = document.querySelector('#todo-edit-list');
        const save = document.querySelector('#save-todo-edit');
        const panel = document.querySelector('.todo-panel');
        const viewport = visualViewport;
        const rect = save.getBoundingClientRect(), panelRect = panel.getBoundingClientRect();
        return { scrollable: list.scrollHeight > list.clientHeight, listHeight: list.clientHeight,
          saveTop: rect.top, saveBottom: rect.bottom, panelTop: panelRect.top, panelBottom: panelRect.bottom,
          viewportTop: viewport.offsetTop, viewportBottom: viewport.offsetTop + viewport.height,
          hitSave: document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2) === save };
      });
      assert(metrics.scrollable && metrics.listHeight > 25, JSON.stringify(metrics));
      assert(metrics.saveTop >= metrics.viewportTop && metrics.saveBottom <= metrics.viewportBottom, JSON.stringify(metrics));
      assert(metrics.panelTop >= metrics.viewportTop && metrics.panelBottom <= metrics.viewportBottom, JSON.stringify(metrics));
      assert(metrics.hitSave, JSON.stringify(metrics));
    }
    for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 568 },
      { width: 390, height: 360 }, { width: 844, height: 390 }]) {
      await phone.setViewportSize(viewport);
      await phone.waitForFunction(() => Math.abs(parseFloat(document.querySelector('#todo-modal').style.getPropertyValue('--todo-viewport-height')) - visualViewport.height) < 1);
      await checkModalFits();
    }
    await phone.setViewportSize({ width: 390, height: 700 });
    await phone.waitForFunction(() => parseFloat(document.querySelector('#todo-modal').style.getPropertyValue('--todo-viewport-height')) === visualViewport.height);
    const listRect = await phone.locator('#todo-edit-list').boundingBox();
    await touch('touchStart', listRect.x + 5, listRect.y + listRect.height - 20);
    for (let step = 1; step <= 6; step++) {
      await touch('touchMove', listRect.x + 5, listRect.y + listRect.height - 20 - step * Math.min(25, (listRect.height - 40) / 6));
    }
    await touch('touchEnd');
    await phone.waitForFunction(() => document.querySelector('#todo-edit-list').scrollTop > 0);
    await checkModalFits();
    const lastInput = phone.locator('#todo-edit-list textarea').last();
    await lastInput.scrollIntoViewIfNeeded(); await lastInput.focus(); await lastInput.press('End');
    await lastInput.press('Enter');
    await phone.locator('#todo-edit-list textarea').last().fill('新增后仍可点击保存');
    await checkModalFits();
    await phone.locator('#save-todo-edit').tap();
    await phone.waitForFunction(() => document.querySelector('#todo-modal').hidden);
    const savedLongTodo = (await api('/api/entries')).entries.find(e => e.id === longTodo.id);
    assert.equal(savedLongTodo.items.length, 36);
    assert.equal(savedLongTodo.items.at(-1).content, '新增后仍可点击保存');
    assert.deepEqual(errors, []);
    console.log('PASS: unchanged appearance, original-node todo/text editing, multiline/paste, validation, IME, failure/retry, switching, refresh race, stage, keyboard/mouse sorting, persistence; mobile tap, hold, cancel, scrolling, bulk editor, long-list scrolling and visible Save in small/keyboard viewports');
    await mobile.close();
  } finally {
    if (browser) await browser.close();
    server.kill(); await new Promise(resolve => server.once('exit', resolve)); rmSync(dir, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
