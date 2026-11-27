/* Uses only a random memory_test_* database and temporary attachment directory. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn,spawnSync} = require('node:child_process');
const {mkdtempSync,rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');
const sleep = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
 const dir=mkdtempSync(path.join(tmpdir(),'memory-search-'));
 const mongo=new URL(process.env.TEST_MONGO_URI);mongo.pathname='/memory_test_search_browser_'+require('node:crypto').randomBytes(10).toString('hex');
 const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
 const env={...process.env,HOST:'127.0.0.1',PORT:String(port),DATA_DIR:dir,MONGO_URI:mongo.toString()};
 const server=spawn('python3',['server.py'],{cwd:path.resolve(__dirname,'..'),env,stdio:'ignore'});
 const base='http://127.0.0.1:'+port;let browser;
 try {
  let ready=false;
  for(let i=0;i<60;i++){try{ready=(await fetch(base+'/health')).ok;}catch{}if(ready)break;await sleep(100);}assert(ready);
  const api=async(url,body)=>{const r=await fetch(base+url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});assert(r.ok);return r.json();};
  await api('/api/entries',{content:'上海 MongoDB a.*'});
  await api('/api/entries',{type:'todo',items:['客户拜访','检查备份']});
  await api('/api/entries',{content:'北京会议'});
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
  const page=await browser.newPage({viewport:{width:1100,height:850}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base);await page.waitForFunction(()=>document.querySelectorAll('#history article').length===3);
  const search=page.locator('#memory-search');
  await search.fill('上海 mongodb');await page.waitForFunction(()=>document.querySelector('#entry-count').textContent==='1 条匹配');
  assert.match(await page.locator('#history').innerText(),/上海 MongoDB/);assert(await page.locator('#clear-button').isHidden());
  assert.deepEqual(await page.locator('#history .search-highlight').allTextContents(),['上海','MongoDB']);
  assert.equal(await search.evaluate(node=>getComputedStyle(node).fontSize),'14px');
  await search.fill('a.*');await page.waitForFunction(()=>document.querySelector('#history .search-highlight')?.textContent==='a.*');
  assert.deepEqual(await page.locator('#history .search-highlight').allTextContents(),['a.*']);
  await search.fill('.*不存在');await page.getByText('没有找到匹配的记忆',{exact:true}).waitFor();
  await page.locator('#clear-search').click();await page.waitForFunction(()=>document.querySelectorAll('#history article').length===3);assert(await page.locator('#clear-button').isVisible());
  // A delayed old search response must not replace a newer query's results.
  let release, entered;const held=new Promise(r=>release=r);const intercepted=new Promise(r=>entered=r);
  await page.route('**/api/entries?q=*',async route=>{if(new URL(route.request().url()).searchParams.get('q')==='上海'){const response=await route.fetch();entered();await held;await route.fulfill({response});}else await route.continue();});
  await search.fill('上海');await intercepted;await search.fill('北京');await page.waitForFunction(()=>document.querySelector('#entry-count').textContent==='1 条匹配' && document.querySelector('#history').textContent.includes('北京会议'));
  release();await sleep(200);assert.match(await page.locator('#history').innerText(),/北京会议/);assert(!/上海 MongoDB/.test(await page.locator('#history').innerText()));
  await page.unroute('**/api/entries?q=*');
  await search.fill('备份');await page.waitForFunction(()=>document.querySelectorAll('#history .todo-item').length===2);
  assert.deepEqual(await page.locator('#history .todo-item .search-highlight').allTextContents(),['备份']);
  // Editing the matched item removes it from results while preserving the query.
  await page.getByText('检查备份',{exact:true}).click();const input=page.locator('.todo-item-text[contenteditable]');await input.fill('检查完成');await input.press('Control+Enter');
  await page.getByText('没有找到匹配的记忆',{exact:true}).waitFor();assert.equal(await search.inputValue(),'备份');
  await search.press('Escape');await page.waitForFunction(()=>document.querySelectorAll('#history article').length===3);
  const mobile=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,deviceScaleFactor:2});const phone=await mobile.newPage();await phone.goto(base);await phone.locator('#memory-search').fill('客户');await phone.waitForFunction(()=>document.querySelector('#entry-count').textContent==='1 条匹配');
  assert(await phone.locator('#clear-search').isVisible());assert(await phone.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await phone.screenshot({path:path.join(dir,'memory-search-mobile.png'),fullPage:true});
  await mobile.close();
  const imageResponse=await fetch(base+'/api/images',{method:'POST',headers:{'Content-Type':'image/png','X-Filename':'sample.png','X-Content':encodeURIComponent('前缀关键词后缀'),'X-Image-Position':'0'},body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64')});assert(imageResponse.ok);
  const imageId=(await imageResponse.json()).entry.id;
  await search.fill('关键词');await page.waitForFunction(()=>document.querySelector('#history .image-caption .search-highlight')?.textContent==='关键词');
  await page.locator('#history .image-caption .search-highlight').click();
  await page.locator('#history .mixed-inline-editor').waitFor();
  assert.equal(await page.locator('#history .mixed-inline-editor .search-highlight').count(),0);
  await page.locator('#history .mixed-inline-editor').press('Escape');
  assert.equal((await (await fetch(base+'/api/entries?q='+encodeURIComponent('关键词'))).json()).entries.find(item=>item.id===imageId).content,'前缀关键词后缀');
  assert.deepEqual(errors,[]);
  console.log('PASS: desktop/mobile search, keyword combinations, clear, empty result, complete todo list, stale-response guard, live edits, no horizontal overflow');
 }finally{
  if(browser)await browser.close();server.kill();await new Promise(r=>server.once('exit',r));
  spawnSync('python3',['-c','import os;from pymongo import MongoClient;c=MongoClient(os.environ["MONGO_URI"]);d=c.get_default_database();assert d.name.startswith("memory_test_");c.drop_database(d.name);c.close()'],{env});rmSync(dir,{recursive:true,force:true});
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
