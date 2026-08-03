import fs from "node:fs";

const html = fs.readFileSync(new URL("../crm.html", import.meta.url), "utf8");
if (!html.includes("</html>")) throw new Error("Release CRM HTML is incomplete");

const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)];
if (!scripts.length) throw new Error("Release CRM script was not found");
new Function(scripts.at(-1)[1]);

const requiredSections = ["page-today", "page-calendar", "page-tasks", "page-team", "page-control", "page-more"];
for (const section of requiredSections) {
  if (!html.includes(`id="${section}"`)) throw new Error(`Missing release CRM section: ${section}`);
}

const requiredApi = [
  "/api/admin/crm/context", "/api/admin/crm/overview", "/api/admin/crm/calendar",
  "/api/admin/crm/tasks", "/api/admin/crm/employees", "/api/admin/crm/data-quality",
  "/api/admin/crm/planned-shifts", "/api/admin/crm/shifts", "/api/admin/crm/trends",
];
for (const endpoint of requiredApi) {
  if (!html.includes(endpoint)) throw new Error(`Missing release CRM API: ${endpoint}`);
}

const requiredSafety = [
  'task.status==="published"', "bb_crm_admin_token", "X-Admin-Token", "Authorization",
  'id="taskRequiresPhoto"', 'requires_photo:$("taskRequiresPhoto").checked',
];
for (const marker of requiredSafety) {
  if (!html.includes(marker)) throw new Error(`Missing release CRM safety marker: ${marker}`);
}

console.log("PASS release CRM: complete HTML, valid JS, routes, APIs and published-only calendar counters");
