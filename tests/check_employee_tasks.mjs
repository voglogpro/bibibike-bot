import fs from "node:fs";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)];
if (!scripts.length) throw new Error("Mini App script was not found");
new Function(scripts.at(-1)[1]);

for (const id of ["btnMyTasks", "myTasksBadge", "myTasksView", "myTasksList", "myTaskDetail", "btnOpenCrm"]) {
  if (!html.includes(`id="${id}"`)) throw new Error(`Missing employee task element: ${id}`);
}
for (const endpoint of ["/api/crm/tasks/mine", "/api/crm/tasks/${id}/progress", "/api/crm/tasks/${id}/attachments"]) {
  if (!html.includes(endpoint)) throw new Error(`Missing employee task API: ${endpoint}`);
}
for (const marker of ["task.requires_photo", "FormData", "URL.createObjectURL", "Authorization", "image/webp"]) {
  if (!html.includes(marker)) throw new Error(`Missing employee task flow marker: ${marker}`);
}
console.log("PASS employee tasks: valid JS, DOM, APIs, multipart and authenticated photo rendering");
