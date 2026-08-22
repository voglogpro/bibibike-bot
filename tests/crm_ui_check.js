const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(path.join(__dirname, "..", "crm.html"), "utf8");
const match = source.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
if (!match) throw new Error("CRM script block not found");
new Function(match[1]);

for (const required of [
  'data-route="map"',
  'id="page-map"',
  "if(state.route===\"map\"&&route!==\"map\")destroyMap()",
  "/statistics-visibility",
]) {
  if (!source.includes(required)) throw new Error(`CRM contract missing: ${required}`);
}

console.log("PASS CRM UI: JavaScript syntax, map route cleanup and archive controls");
