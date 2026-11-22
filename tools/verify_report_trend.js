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
    const calendar = document.querySelector("#careerCalendarStage svg.career-calendar");
    const actions = document.getElementById("careerTrendActions");
    return {
      days: Number(svg.dataset.days),
      start: Number(svg.dataset.start),
      actions: actions.classList.contains("on"),
      xhit: Boolean(svg.querySelector(".x-axis-hit")),
      calendarDays: Number(calendar?.dataset.days || 0),
      calendarText: document.getElementById("careerCalendarMeta")?.textContent || "",
    };
  });

  const box = await page.locator("#careerTrendStage svg.career-trend").boundingBox();
  await page.mouse.click(box.x + box.width * 0.62, box.y + box.height * (242 / 260));
  await page.waitForTimeout(120);
  const filtered = await page.evaluate(() => {
    const svg = document.querySelector("#careerTrendStage svg.career-trend");
    const calendar = document.querySelector("#careerCalendarStage svg.career-calendar");
    const actions = document.getElementById("careerTrendActions");
    return {
      days: Number(svg.dataset.days),
      start: Number(svg.dataset.start),
      actions: actions.classList.contains("on"),
      range: document.getElementById("careerTrendRange").textContent,
      calendarDays: Number(calendar?.dataset.days || 0),
      calendarText: document.getElementById("careerCalendarMeta")?.textContent || "",
    };
  });
  await page.locator(".trend-panel").screenshot({ path: "trend_filtered.png" });

  const calendarTarget = await page.evaluate(() => {
    const cells = Array.from(document.querySelectorAll("#careerCalendarStage .cell"));
    const cell = cells[Math.max(0, Math.floor(cells.length * 0.7))];
    const index = Number(cell.dataset.calendarIndex);
    cell.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    return index;
  });
  await page.waitForTimeout(120);
  const calendarDriven = await page.evaluate(() => {
    const svg = document.querySelector("#careerTrendStage svg.career-trend");
    const calendar = document.querySelector("#careerCalendarStage svg.career-calendar");
    return {
      start: Number(svg.dataset.start),
      days: Number(svg.dataset.days),
      calendarDays: Number(calendar.dataset.days),
      calendarText: document.getElementById("careerCalendarMeta").textContent,
    };
  });

  await page.locator("#careerTrendReset").click();
  await page.waitForTimeout(120);
  const restored = await page.evaluate(() => {
    const svg = document.querySelector("#careerTrendStage svg.career-trend");
    const calendar = document.querySelector("#careerCalendarStage svg.career-calendar");
    const actions = document.getElementById("careerTrendActions");
    return {
      days: Number(svg.dataset.days),
      start: Number(svg.dataset.start),
      actions: actions.classList.contains("on"),
      range: document.getElementById("careerTrendRange").textContent,
      calendarDays: Number(calendar?.dataset.days || 0),
      calendarText: document.getElementById("careerCalendarMeta")?.textContent || "",
    };
  });

  await page.goto("file:///" + monthReport.replace(/\\/g, "/"), { waitUntil: "load" });
  const monthly = await page.evaluate(() => ({
    xhit: document.querySelectorAll(".career-trend .x-axis-hit").length,
    reset: Boolean(document.getElementById("careerTrendReset")),
    calendarStage: Boolean(document.getElementById("careerCalendarStage")),
  }));

  const result = {
    avatar,
    before,
    filtered,
    calendarTarget,
    calendarDriven,
    restored,
    monthly,
  };
  console.log(JSON.stringify(result, null, 2));
  if (!avatar || avatar.width <= 0 || avatar.height <= 0) {
    throw new Error("Report avatar failed to load");
  }
  if (!before.xhit || !filtered.actions || filtered.days >= before.days || filtered.start <= 0) {
    throw new Error("Career trend filtering failed");
  }
  if (!before.calendarDays || before.calendarDays !== before.days) {
    throw new Error("Career calendar initial range is not synchronized");
  }
  if (filtered.calendarDays !== filtered.days || !filtered.calendarText.includes("起")) {
    throw new Error("Career calendar did not follow trend filtering");
  }
  if (calendarDriven.start !== calendarTarget ||
      calendarDriven.days !== before.days - calendarTarget ||
      calendarDriven.calendarDays !== calendarDriven.days ||
      !calendarDriven.calendarText.includes("起")) {
    throw new Error("Calendar click did not drive career trend");
  }
  if (restored.actions || restored.days !== before.days || restored.start !== 0) {
    throw new Error("Career trend reset failed");
  }
  if (restored.calendarDays !== before.calendarDays) {
    throw new Error("Career calendar did not restore");
  }
  if (monthly.xhit !== 0 || monthly.reset || monthly.calendarStage) {
    throw new Error("Monthly report unexpectedly became interactive");
  }
  await browser.close();
})();
