// T2 capture v5: locate-me → Krasnoyarsk pickup list appears → click a pickup point row →
// confirm ("Привезти сюда"/"Выбрать") → region flips to Krasnoyarsk → save state.
import { chromium } from "playwright";
import { mkdirSync } from "fs";
const mode = process.argv[2] || "headless";
const HOME = "https://www.ozon.ru/";
const API = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=";
const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const KR = { latitude: 56.0106, longitude: 92.8526, accuracy: 30 };
const OUT = "/tmp/ozcap"; mkdirSync(OUT, { recursive: true });
const log = (...a) => console.error("[cap]", ...a);

const browser = await chromium.launch({ headless: mode==="headless", args:["--disable-blink-features=AutomationControlled","--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu","--mute-audio","--no-first-run","--no-default-browser-check"] });
const ctx = await browser.newContext({ viewport:{width:1920,height:1080}, userAgent: UA, locale:"ru-RU", permissions:["geolocation"] });
const page = await ctx.newPage();
const shot = (p) => page.screenshot({path:`${OUT}/${p}.png`}).catch(()=>{});
const calls=[];
page.on("request",r=>{const u=r.url();if(/_action/i.test(u)&&/deliveryLocation|saveAddress|selectPickup|setDelivery|savePin|address|Location|Pickup/i.test(u)){let b="";try{b=r.postData()?.slice(0,180)||""}catch{};calls.push(`${r.method()} ${u.slice(30,120)} ${b}`);}});

log("1. home + antibot..."); await page.goto(HOME,{waitUntil:"domcontentloaded",timeout:90000}); await page.waitForTimeout(12000);
const title=await page.title(); log("   title:", title.slice(0,40));
if(/соединени|antibot/i.test(title)){ log("   ✗ ANTIBOT FAILED — abort"); await browser.close(); process.exit(1); }
async function get(u){ return page.evaluate(async(x)=>{const r=await fetch(x,{headers:{accept:"application/json"}});return{status:r.status,text:await r.text()};},u); }
const marker=t=>["Красноярск","Москва","Санкт-Петербург","Новосибирск"].find(c=>t.includes(c))||"?";
log("   region BEFORE:", marker((await get(API+encodeURIComponent("/"))).text));

await ctx.setGeolocation(KR); log("2. geolocation → Krasnoyarsk");
try{const ok=await page.$('button:has-text("ОК")');if(ok){await ok.click();await page.waitForTimeout(1000);}}catch{}
try{const trig=await page.$('[data-widget="addressBar"]')||await page.$('text=Укажите адрес')||await page.$('button:has-text("Москва")');if(trig){await trig.click();await page.waitForTimeout(2500);}}catch{}
try{ await page.getByText("Выбрать на карте",{exact:false}).first().click({timeout:8000}); await page.waitForTimeout(6000); log("3. map opened");}catch(e){log("   map err",e.message.slice(0,50));}
try{ await page.getByText(/Определить местоположение/i).first().click({timeout:8000}); log("4. locate-me clicked"); await page.waitForTimeout(6000);}catch(e){log("   locate err",e.message.slice(0,50));}
await shot("v5-located");

// Click the FIRST Krasnoyarsk pickup point in the list panel
log("5. click first Krasnoyarsk pickup point...");
try{
  const pt = page.getByText(/Пункт Ozon/i).first();
  if(await pt.count()){ await pt.click({timeout:6000}); log("   clicked pickup row"); await page.waitForTimeout(4000); }
  else { // fallback: click a "Красноярск" address line
    const kr = page.getByText(/Красноярск/).first(); if(await kr.count()){ await kr.click({timeout:6000}); log("   clicked krsk row"); await page.waitForTimeout(4000);} else log("   no pickup row found");
  }
}catch(e){log("   pickup click err",e.message.slice(0,60));}
await shot("v5-picked");

// Confirm
log("6. confirm...");
try{
  for(const label of ["Привезти сюда","Доставить сюда","Заберу отсюда","Выбрать этот","Выбрать","Сохранить","Подтвердить","Готово"]){
    const b=page.getByRole('button',{name:new RegExp(label)});
    if(await b.count()&&await b.first().isVisible().catch(()=>false)){ await b.first().click().catch(()=>{}); log("   confirm:",label); await page.waitForTimeout(4000); break; }
  }
}catch(e){log("   confirm err",e.message.slice(0,50));}
await shot("v5-after");

log("7. calls:", JSON.stringify([...new Set(calls)].slice(0,8)));
await page.goto(HOME,{waitUntil:"domcontentloaded",timeout:60000}); await page.waitForTimeout(2000);
const after=marker((await get(API+encodeURIComponent("/"))).text);
log("8. region AFTER:", after);
if(after==="Красноярск"){ await ctx.storageState({path:"/tmp/krsk-state.json"}); log("   ✓✓ SAVED /tmp/krsk-state.json"); }
else log("   ✗ still "+after);
await browser.close(); log("DONE");
