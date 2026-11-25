/* Runs only against a random memory_test_* database and temporary attachment directory. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn,spawnSync} = require('node:child_process');
const {mkdtempSync,rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'memory-mixed-'));
  const mongo = new URL(process.env.TEST_MONGO_URI);
  mongo.pathname = '/memory_test_mixed_browser_' + require('node:crypto').randomBytes(10).toString('hex');
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const env = {...process.env, HOST:'127.0.0.1', PORT:String(port), DATA_DIR:dir, MONGO_URI:mongo.toString()};
  const server = spawn('python3', ['server.py'], {cwd:path.resolve(__dirname, '..'), env, stdio:'ignore'});
  const base = `http://127.0.0.1:${port}`;
  let browser;
  try {
    let ready = false;
    for (let i=0; i<60; i++) { try { ready = (await fetch(base+'/health')).ok; } catch {} if (ready) break; await sleep(100); }
    assert(ready);
    browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
    const page = await browser.newPage({viewport:{width:1100,height:850}});
    const errors = []; const deletes = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.method()==='DELETE' && new URL(request.url()).pathname==='/api/entries') deletes.push(request.url()); });
    await page.goto(base);
    await page.locator('#content').fill('第一段');
    async function pasteImage(target, name) {
      await target.evaluate((node, input) => {
        node.focus();
        const bytes = Uint8Array.from(atob(input.png), char => char.charCodeAt(0));
        const file = new File([bytes], input.name, {type:'image/png'});
        const transfer = new DataTransfer(); transfer.items.add(file);
        node.dispatchEvent(new ClipboardEvent('paste', {bubbles:true,cancelable:true,clipboardData:transfer}));
      }, {png, name});
    }
    await pasteImage(page.locator('#content'), 'first.png');
    await page.locator('#content .inline-draft-image').first().waitFor();
    await page.locator('#content').press('End');
    await page.keyboard.type('第二段');
    await pasteImage(page.locator('#content'), 'second.png');
    await page.waitForFunction(() => document.querySelectorAll('#content .inline-draft-image').length === 2);
    await page.locator('#content').press('End');
    await page.keyboard.type('第三段\n第四段');
    await page.locator('#save-button').click();
    await page.waitForFunction(() => document.querySelectorAll('#history .image-preview').length === 2);
    const documentView = page.locator('#history .image-document').first();
    assert.equal(await documentView.locator('.document-image').count(), 2);
    assert.equal(await documentView.locator('.image-details, .kind-badge, time').count(), 0);
    assert.equal(await documentView.getByText('下载', {exact:true}).count(), 0);
    assert.equal(await documentView.getByText('first.png', {exact:true}).count(), 0);
    assert.equal(await documentView.getByText('second.png', {exact:true}).count(), 0);
    const readingStyle = await documentView.locator('.image-content').evaluate(node => ({
      background: getComputedStyle(node).backgroundColor,
      padding: getComputedStyle(node).paddingLeft,
    }));
    await documentView.locator('.document-image').first().click();
    await documentView.locator('.mixed-inline-editor').waitFor();
    const editingStyle = await documentView.locator('.mixed-inline-editor').evaluate(node => ({
      background: getComputedStyle(node).backgroundColor,
      padding: getComputedStyle(node).paddingLeft,
    }));
    assert.deepEqual(editingStyle, readingStyle);
    await documentView.locator('.mixed-inline-editor').press('Escape');
    let saved = (await (await fetch(base+'/api/entries')).json()).entries[0];
    assert.equal(saved.images.length, 2);
    assert.match(saved.content, /第一段/);
    assert.match(saved.content, /第四段/);
    await page.locator('#history .image-caption').last().click();
    const editor = page.locator('#history .mixed-inline-editor');
    await editor.waitFor();
    await editor.press('End');
    await page.keyboard.type('补充文字');
    await pasteImage(editor, 'third.png');
    await page.waitForFunction(() => document.querySelectorAll('#history .mixed-inline-editor .inline-draft-image').length === 3);
    await page.locator('#history-title').click();
    await page.waitForFunction(() => document.querySelectorAll('#history .image-preview').length === 3);
    saved = (await (await fetch(base+'/api/entries')).json()).entries[0];
    assert.equal(saved.images.length, 3);
    assert.match(saved.content, /补充文字/);
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('#history .image-preview').length === 3);
    await page.locator('#history .image-caption').first().click();
    await page.locator('#history .mixed-inline-editor').press('End');
    await page.keyboard.type('取消的内容');
    await page.locator('#history .mixed-inline-editor').press('Escape');
    assert(!((await (await fetch(base+'/api/entries')).json()).entries[0].content.includes('取消的内容')));
    let failedSave = false;
    await page.route('**/api/entries/*', async route => {
      if (route.request().method()==='PATCH' && !failedSave) {
        failedSave = true; await route.fulfill({status:503,contentType:'application/json',body:'{"error":"模拟断线"}'});
      } else await route.continue();
    });
    await page.locator('#history .image-caption').last().click();
    await page.locator('#history .mixed-inline-editor').press('End');
    await page.keyboard.type('断线后保留');
    await page.locator('#history-title').click();
    await page.getByText(/模拟断线；草稿仍在/).waitFor();
    await page.locator('#history-title').click();
    await page.waitForFunction(() => !document.querySelector('#history .mixed-inline-editor'));
    assert(((await (await fetch(base+'/api/entries')).json()).entries[0].content.includes('断线后保留')));
    await page.unroute('**/api/entries/*');
    await page.locator('#content').fill('普通文字记录');
    await page.locator('#save-button').click();
    await page.waitForFunction(() => document.querySelectorAll('#history article').length === 2);
    await page.locator('#history article').first().locator('pre[data-entry-id]').click();
    await pasteImage(page.locator('#history article').first().locator('pre[contenteditable]'), 'inside-text.png');
    await page.waitForFunction(() => document.querySelectorAll('#history article .mixed-inline-editor .inline-draft-image').length === 1);
    await page.locator('#history-title').click();
    await page.waitForFunction(() => document.querySelectorAll('#history .image-preview').length === 4);
    const converted = (await (await fetch(base+'/api/entries')).json()).entries.find(item => item.content.includes('普通文字记录'));
    assert.equal(converted.images.length, 1);
    const legacyResponse = await fetch(base+'/api/images', {
      method:'POST',
      headers:{'Content-Type':'image/png','X-Filename':'legacy.png','X-Content':encodeURIComponent('旧图下方文字'),'X-Image-Position':'0'},
      body:Buffer.from(png,'base64'),
    });
    assert(legacyResponse.ok);
    const legacyId = (await legacyResponse.json()).entry.id;
    await page.reload();
    await page.locator(`.image-caption[data-entry-id="${legacyId}"]`).click();
    await page.locator('#history .mixed-inline-editor').waitFor();
    await pasteImage(page.locator('#history .mixed-inline-editor'), 'legacy-second.png');
    await page.waitForFunction(() => document.querySelectorAll('#history .mixed-inline-editor .inline-draft-image').length === 2);
    await page.locator('#history-title').click();
    await page.waitForFunction(() => !document.querySelector('#history .mixed-inline-editor'));
    const legacyNow = (await (await fetch(base+'/api/entries')).json()).entries.find(item => item.id===legacyId);
    assert.equal(legacyNow.images.length, 2);
    assert.deepEqual(deletes, []);
    assert.deepEqual(errors, []);
    const phone = await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
    await phone.goto(base);
    await phone.waitForFunction(() => document.querySelectorAll('#history .image-preview').length === 6);
    assert(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    if (process.env.MIXED_SCREENSHOT_PATH) await phone.locator('.image-document').first().screenshot({path:process.env.MIXED_SCREENSHOT_PATH});
    await phone.close();
    console.log('PASS: multi-image text flow, inline blur save, refresh, mobile width, no clear-all request');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      server.kill();
      await new Promise(resolve => server.once('exit', resolve));
    }
    spawnSync('python3', ['-c', 'import os;from pymongo import MongoClient;c=MongoClient(os.environ["MONGO_URI"]);d=c.get_default_database();assert d.name.startswith("memory_test_");c.drop_database(d.name);c.close()'], {env});
    rmSync(dir, {recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode=1; });
