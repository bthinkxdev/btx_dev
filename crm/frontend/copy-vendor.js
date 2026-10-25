// Copies pinned third-party browser builds into crm/static/crm/vendor so the
// app never depends on a CDN at runtime.
const fs = require("fs");
const path = require("path");
const out = path.resolve(__dirname, "../static/crm/vendor");
fs.mkdirSync(out, { recursive: true });
const files = {
  "alpinejs/dist/cdn.min.js": "alpine.min.js",
  "apexcharts/dist/apexcharts.min.js": "apexcharts.min.js",
};
for (const [src, dest] of Object.entries(files)) {
  fs.copyFileSync(path.resolve(__dirname, "node_modules", src), path.join(out, dest));
  console.log("copied", dest);
}
