const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const repo = "C:/Users/sam/toast-webhook-deploy";
const promptsPath = path.join(repo, "audit_outputs", "prod_assistant_smoke_prompts_102_from_c038b27.json");
const outPath = path.join(repo, "audit_outputs", "prod_assistant_smoke_2026-06-07_20616bc_direct.json");
const mirrorPath = path.join(repo, "audit_outputs", "prod_assistant_smoke_2026-06-07_20616bc_direct_mirror.json");
const summaryPath = path.join(repo, "audit_outputs", "prod_assistant_smoke_2026-06-07_20616bc_direct_summary.json");

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const startN = Number(argValue("--start", "1"));
const endN = Number(argValue("--end", "102"));
const doMirror = process.argv.includes("--mirror");

function loadJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function saveJson(file, data) {
  fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n", "utf8");
}

function answerFrom(data) {
  if (!data || typeof data !== "object") return "";
  return String(data.answer || data.error || "");
}

function parseMirrorMessage(message) {
  const content = String(message.content || "");
  const prefix = "CENAS_ASSISTANT_REVIEW_V2\n";
  if (!content.startsWith(prefix)) return null;
  try {
    const payload = JSON.parse(content.slice(prefix.length));
    const turn = payload.turn || {};
    const tool = payload.tool || {};
    const result = payload.result || {};
    return {
      message_id: message.id,
      created_at: message.created_at,
      asked_at: payload.asked_at,
      question: turn.question || "",
      answer: turn.answer || "",
      route_path: tool.route_path || payload.telemetry?.route_path || "",
      tool_id: tool.id || "",
      routed_tool_id: tool.routed_tool_id || "",
      final_tool_id: tool.final_tool_id || tool.id || tool.routed_tool_id || "",
      http_status: result.http_status,
      ok: result.ok,
      queued: result.queued,
      reason: result.reason,
      error: result.error,
      raw_response: payload.raw_response || "",
    };
  } catch (error) {
    return { message_id: message.id, parse_error: String(error), content: content.slice(0, 500) };
  }
}

async function main() {
  const prompts = loadJson(promptsPath, []);
  if (!Array.isArray(prompts) || prompts.length !== 102) {
    throw new Error(`expected 102 prompts, found ${prompts.length}`);
  }
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9222");
  const context = browser.contexts()[0];
  const page = context.pages().find((p) => p.url().includes("app.cenaskitchen.com")) || context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(30000);
  await page.goto("https://app.cenaskitchen.com/assistant", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1000);
  const bodyText = await page.locator("body").innerText().catch(() => "");
  if (/ENTER PIN|PHONE|keypad-login/i.test(bodyText) || page.url().includes("keypad-login")) {
    throw new Error(`Chrome session is not authenticated: ${page.url()}`);
  }
  const html = await page.content();
  const markers = [...new Set([...html.matchAll(/\?v=([a-f0-9]{7})/g)].map((m) => m[1]))];

  const state = loadJson(outPath, {
    commit: "20616bc",
    parent_fix: "8fd272d",
    started_at: new Date().toISOString(),
    build_markers: markers,
    results: [],
  });
  state.build_markers = markers;
  const done = new Map((state.results || []).map((row) => [Number(row.n), row]));
  let previousQuestion = "";
  let previousAnswer = "";
  for (const row of [...done.values()].sort((a, b) => Number(a.n) - Number(b.n))) {
    if (Number(row.n) < startN) {
      previousQuestion = row.question || "";
      previousAnswer = row.answer || "";
    }
  }

  for (const prompt of prompts.filter((p) => Number(p.n) >= startN && Number(p.n) <= endN)) {
    const n = Number(prompt.n);
    if (done.has(n)) {
      previousQuestion = prompt.question || "";
      previousAnswer = done.get(n).answer || "";
      console.log(`skip ${n}/102 ${prompt.id}`);
      continue;
    }
    const payload = {
      question: prompt.question,
      previous_question: previousQuestion,
      previous_answer: previousAnswer,
    };
    const started = new Date().toISOString();
    let response;
    try {
      response = await page.evaluate(async (payload) => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 70000);
        try {
          const res = await fetch("/assistant/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "application/json" },
            body: JSON.stringify(payload),
            signal: controller.signal,
          });
          const text = await res.text();
          let json = null;
          try { json = JSON.parse(text); } catch {}
          return { http_status: res.status, text, json };
        } finally {
          clearTimeout(timer);
        }
      }, payload);
    } catch (error) {
      response = { http_status: 0, text: "", json: null, error: String(error) };
    }
    const data = response.json || {};
    const result = {
      n,
      id: prompt.id,
      stage: prompt.stage,
      question: prompt.question,
      asked_at_local: started,
      http_status: response.http_status,
      ok: data.ok,
      queued: data.queued,
      route_path: data.route_path || "",
      tool_id: data.tool_id || "",
      routed_tool_id: data.routed_tool_id || "",
      final_tool_id: data.tool_id || data.routed_tool_id || "",
      reason: data.reason || "",
      error: data.error || response.error || "",
      answer: answerFrom(data),
      raw_text: response.text ? response.text.slice(0, 2000) : "",
    };
    state.results.push(result);
    state.results.sort((a, b) => Number(a.n) - Number(b.n));
    saveJson(outPath, state);
    previousQuestion = prompt.question || "";
    previousAnswer = result.answer || "";
    console.log(`${n}/102 ${prompt.id} status=${result.http_status} route=${result.route_path || ""} tool=${result.final_tool_id || ""} queued=${result.queued} ${result.error || ""}`);
    await page.waitForTimeout(250);
  }

  if (doMirror) {
    const since = state.started_at;
    const mirror = await page.evaluate(async (since) => {
      const qs = new URLSearchParams({ limit: "300", include_all: "true", since });
      const res = await fetch(`/sam/cena/sam-chat?${qs.toString()}`, { headers: { "Accept": "application/json" } });
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch {}
      return { http_status: res.status, text, json };
    }, since);
    const messages = mirror.json?.messages || [];
    const parsed = messages.map(parseMirrorMessage).filter(Boolean);
    saveJson(mirrorPath, { since, http_status: mirror.http_status, count: parsed.length, rows: parsed });
    const byQuestion = new Map();
    for (const row of parsed) {
      if (!byQuestion.has(row.question)) byQuestion.set(row.question, []);
      byQuestion.get(row.question).push(row);
    }
    const joined = state.results.map((result) => {
      const rows = byQuestion.get(result.question) || [];
      const match = rows.shift() || null;
      return { ...result, mirror: match };
    });
    const missing = joined.filter((row) => !row.mirror).map((row) => row.id);
    const httpBad = joined.filter((row) => row.http_status !== 200).map((row) => row.id);
    const dangerousBad = joined.filter((row) => /^A(2[0-9]|19)$/.test(row.id || "") && row.mirror && (row.mirror.route_path !== "review" || row.mirror.final_tool_id));
    saveJson(summaryPath, {
      commit: state.commit,
      parent_fix: state.parent_fix,
      build_markers: state.build_markers,
      total: state.results.length,
      mirror_rows: parsed.length,
      mirror_missing: missing.length,
      missing,
      http_bad: httpBad,
      dangerous_bad: dangerousBad.map((row) => row.id),
      finished_at: new Date().toISOString(),
    });
    console.log(`mirror rows=${parsed.length} missing=${missing.length} http_bad=${httpBad.length} dangerous_bad=${dangerousBad.length}`);
  }

  await browser.close();
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
