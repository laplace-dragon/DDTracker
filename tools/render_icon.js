const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

const PLAYWRIGHT = "C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const root = path.resolve(__dirname, "..");
const svgPath = path.join(root, "assets", "app-icon.svg");
const pngPath = path.join(root, "assets", "app-icon.png");
const icoPath = path.join(root, "assets", "app-icon.ico");
const tempDir = path.join(root, "build", "icon-sizes");

async function renderPng(browser, size, output) {
  const page = await browser.newPage({
    viewport: { width: size, height: size },
    deviceScaleFactor: 1,
  });
  await page.goto(pathToFileURL(svgPath).href, { waitUntil: "load" });
  await page.screenshot({
    path: output,
    omitBackground: true,
    clip: { x: 0, y: 0, width: size, height: size },
  });
  await page.close();
}

function buildIco(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);

  const entries = [];
  let offset = 6 + images.length * 16;
  for (const { size, data } of images) {
    const entry = Buffer.alloc(16);
    entry[0] = size === 256 ? 0 : size;
    entry[1] = size === 256 ? 0 : size;
    entry[2] = 0;
    entry[3] = 0;
    entry.writeUInt16LE(1, 4);
    entry.writeUInt16LE(32, 6);
    entry.writeUInt32LE(data.length, 8);
    entry.writeUInt32LE(offset, 12);
    entries.push(entry);
    offset += data.length;
  }
  return Buffer.concat([header, ...entries, ...images.map((item) => item.data)]);
}

(async () => {
  const { chromium } = require(PLAYWRIGHT);
  let browser;
  for (const options of [{ channel: "msedge" }, { channel: "chrome" }, {}]) {
    try {
      browser = await chromium.launch(options);
      break;
    } catch (_) {
      // Try the next installed Chromium target.
    }
  }
  if (!browser) {
    throw new Error("No Chromium browser is available");
  }

  fs.mkdirSync(tempDir, { recursive: true });
  await renderPng(browser, 1024, pngPath);

  const sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256];
  const images = [];
  for (const size of sizes) {
    const file = path.join(tempDir, `app-icon-${size}.png`);
    await renderPng(browser, size, file);
    images.push({ size, data: fs.readFileSync(file) });
  }
  fs.writeFileSync(icoPath, buildIco(images));
  await browser.close();

  for (const file of fs.readdirSync(tempDir)) {
    fs.unlinkSync(path.join(tempDir, file));
  }
  fs.rmdirSync(tempDir);
  console.log(`PNG ${pngPath}`);
  console.log(`ICO ${icoPath}`);
})();
