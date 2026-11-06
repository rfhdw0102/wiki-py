// Local smoke driver; Playwright is test tooling, not a product dependency.
const { chromium } = require(process.argv[2]);
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[], requests=[];
    page.on('pageerror', e=>errors.push(e.message));
    page.on('request', r=>requests.push({method:r.method(),url:r.url(),body:r.url().endsWith('/dingtalk/imports')?r.postData():null}));
    await page.goto('http://localhost:13082/login?next=/dingtalk');
    await page.locator('input[name=email]').fill('smoke@example.com');
    await page.locator('input[name=password]').fill(process.env.SMOKE_PASSWORD);
    await page.getByRole('button',{name:'Sign in',exact:true}).click();
    await page.waitForURL('**/dingtalk');
    await page.getByRole('radio',{name:/mock-a/}).waitFor();
    assert.equal(await page.getByRole('radio',{name:/mock-a/}).isChecked(),false);
    assert.equal(await page.getByRole('radio',{name:/mock-b/}).isChecked(),false);
    await page.getByRole('radio',{name:/mock-b/}).check();
    assert.equal(await page.getByRole('radio',{name:/mock-a/}).isChecked(),false);
    await page.getByRole('radio',{name:/mock-a/}).check();
    if (process.argv[3]==='import') {
      await page.screenshot({path:process.env.SMOKE_DIR+'/mock-list.png',fullPage:true});
      const response=page.waitForResponse(r=>r.url().endsWith('/dingtalk/imports')&&r.request().method()==='POST');
      await page.getByRole('button',{name:'导入所选模拟文档',exact:true}).click();
      const result=await response;
      assert.equal(result.status(),201);
      fs.writeFileSync(process.env.SMOKE_DIR+'/import-result.json',JSON.stringify(await result.json(),null,2));
      await page.getByRole('button',{name:'开始另一次模拟导入'}).waitFor();
      await page.getByRole('link',{name:'到Wiki重新打开',exact:true}).click();
    } else {
      const result=JSON.parse(fs.readFileSync(process.env.SMOKE_DIR+'/import-result.json'));
      await page.goto('http://localhost:13082/wiki?path='+encodeURIComponent(result.page_path));
    }
    await page.getByRole('heading',{name:'模拟文档A：项目说明',exact:true}).waitFor();
    assert.equal(await page.locator('.wiki-body').innerText(),'这是模拟文档 A 的第一段，说明本地导入流程。English: Alpha only.\n\n这是模拟文档 A 的第二段，保留中文、English 和标点；顺序不得改变。\n\n来源：模拟钉钉（未连接真实钉钉）。 [^sim-1] [^sim-2]');
    assert.match(await page.locator('.wiki-document').innerText(),/模拟钉钉 · 未连接真实钉钉/);
    const result=JSON.parse(fs.readFileSync(process.env.SMOKE_DIR+'/import-result.json'));
    assert.equal(await page.getByRole('button',{name:result.page_path,exact:true}).count(),1);
    await page.reload();
    await page.getByRole('heading',{name:'模拟文档A：项目说明',exact:true}).waitFor();
    await page.screenshot({path:process.env.SMOKE_DIR+'/'+process.argv[3]+'-wiki.png',fullPage:true});
    assert.deepEqual(errors,[]);
    assert.equal(requests.filter(r=>r.method==='POST'&&r.url.endsWith('/dingtalk/imports')).length,process.argv[3]==='import'?1:0);
    assert.ok(requests.every(r=>/^http:\/\/localhost:(13082|18082)\//.test(r.url)));
    fs.writeFileSync(process.env.SMOKE_DIR+'/'+process.argv[3]+'-browser.json',JSON.stringify({errors,requests},null,2));
    console.log(process.argv[3]+' browser PASS: login, radios, exact persisted body, badge, normal Wiki, reload; no external requests');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
