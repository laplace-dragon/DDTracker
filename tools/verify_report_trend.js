const path = require("path");
const PW = "C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(PW);

const report = path.resolve(process.argv[2] || "live_report.html");
const monthReport = path.resolve(process.argv[3] || "live_report-month.html");

(async () => {
  let browser;
  for (const options of [{ channel: "msedge" }, { channel: "chrome" }, {}]) {
    try {
      browser = await chromium.launch({ ...options, headless: true });
      break;
    } catch (error) {
      // Try the next installed Chromium runtime.
    }
  }
  if (!browser) throw new Error("No Chromium browser available");

  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  await page.goto("file:///" + report.replace(/\\/g, "/"), { waitUntil: "load" });
  const avatar = await page.evaluate(() => {
    const img = document.querySelector(".hero .avatar img");
    return img ? {
      srcPrefix: img.src.slice(0, 30),
      width: img.naturalWidth,
      height: img.naturalHeight,
    } : null;
  });
  const before = await page.evaluate(() => {
    const svg = document.querySelector("#careerTrendStage svg.career-trend");
    const actions = document.getElementById("careerTrendActions");
    return {
      days: Number(svg.dataset.days),
      start: Number(svg.dataset.start),
      actions: actions.classList.contains("on"),
      xhit: Boolean(svg.querySelector(".x-axis-hit")),
    };
  });

  const box = await page.locator("#careerTrendStage svg.career-trend").boundingBox();
  await page.mouse.click(box.x + box.width * 0.62, box.y + box.height * (242 / 260));
  await page.waitForTimeout(120);
  const filtered = await page.evaluate(() => {
    const svg = document.querySelector("#careerTrendStage svg.career-trend");
    const actions = document.getElementById("careerTrendActions");
    return {
      days: Number(svg.dataset.days),
      start: Number(svg.dataset.start),
      actions: actions.classList.contains("on"),
      range: document.getElementById("careerTrendRange").textContent,
    };
  });
  await page.locator(".trend-panel").screenshot({ path: "trend_filtered.png" });

  await page.locator("#careerTrendReset").click();
  await page.waitForTimeout(120);
  const restored = await page.evaluate(() => {
    const svg = document.querySelector("#careerTrendStage svg.career-trend");
    const actions = document.getElementById("careerTrendActions");
    return {
      days: Number(svg.dataset.days),
      start: Number(svg.dataset.start),
      actions: actions.classList.contains("on"),
      range: document.getElementById("careerTrendRange").textContent,
    };
  });

  await page.goto("file:///" + monthReport.replace(/\\/g, "/"), { waitUntil: "load" });
  const monthly = await page.evaluate(() => ({
    xhit: document.querySelectorAll(".career-trend .x-axis-hit").length,
    reset: Boolean(document.getElementById("careerTrendReset")),
  }));

  const result = { avatar, before, filtered, restored, monthly };
  console.log(JSON.stringify(result, null, 2));
  if (!avatar || avatar.width <= 0 || avatar.height <= 0) {
    throw new Error("Report avatar failed to load");
  }
  if (!before.xhit || !filtered.actions || filtered.days >= before.days || filtered.start <= 0) {
    throw new Error("Career trend filtering failed");
  }
  if (restored.actions || restored.days !== before.days || restored.start !== 0) {
    throw new Error("Career trend reset failed");
  }
  if (monthly.xhit !== 0 || monthly.reset) {
    throw new Error("Monthly report unexpectedly became interactive");
  }
  await browser.close();
})();
