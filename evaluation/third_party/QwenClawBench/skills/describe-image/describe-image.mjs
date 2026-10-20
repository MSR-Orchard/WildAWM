#!/usr/bin/env node

import { readFile, realpath, stat } from "node:fs/promises";

const CONFIG_PATH = process.env.QWENCLAW_VISION_CONFIG || "/home/node/.openclaw/qwenclaw-vision.json";
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

function fail(message) {
  process.stderr.write(`describe-image: ${message}\n`);
  process.exit(1);
}

function mimeType(buffer) {
  if (buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  if (buffer.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff]))) return "image/jpeg";
  if (buffer.subarray(0, 6).toString("ascii") === "GIF87a" || buffer.subarray(0, 6).toString("ascii") === "GIF89a") return "image/gif";
  if (buffer.subarray(0, 4).toString("ascii") === "RIFF" && buffer.subarray(8, 12).toString("ascii") === "WEBP") return "image/webp";
  return null;
}

function responseText(message) {
  if (typeof message?.content === "string") return message.content.trim();
  if (Array.isArray(message?.content)) {
    return message.content.map((item) => typeof item === "string" ? item : item?.text || "").filter(Boolean).join("\n").trim();
  }
  return "";
}

const imageArgument = process.argv[2];
const question = process.argv.slice(3).join(" ").trim();
if (!imageArgument || !question) fail("usage: describe-image.mjs <image-path> <question>");

let config;
let image;
try {
  const resolvedImage = await realpath(imageArgument);
  const imageStat = await stat(resolvedImage);
  if (!imageStat.isFile()) fail("image path is not a regular file");
  if (imageStat.size > MAX_IMAGE_BYTES) fail("image exceeds the 20 MiB limit");
  image = await readFile(resolvedImage);
  config = JSON.parse(await readFile(CONFIG_PATH, "utf8"));
} catch (error) {
  fail(error?.code === "ENOENT" ? "image or VLM configuration does not exist" : "could not read image or VLM configuration");
}

const mime = mimeType(image);
if (!mime) fail("unsupported or invalid image format; expected PNG, JPEG, GIF, or WebP");

const endpoint = String(config.endpoint || "").replace(/\/+$/, "");
if (!endpoint || !config.model || !config.apiKey) fail("VLM configuration is incomplete");
const body = {
  model: config.model,
  messages: [{ role: "user", content: [
    { type: "text", text: question },
    { type: "image_url", image_url: { url: `data:${mime};base64,${image.toString("base64")}` } },
  ] }],
  max_tokens: config.maxOutputTokens,
};
if (config.reasoningEffort) body.reasoning_effort = config.reasoningEffort;

let response;
try {
  response = await fetch(`${endpoint}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${config.apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(config.timeoutSeconds * 1000),
  });
} catch (_error) {
  fail("VLM request failed");
}
if (!response.ok) fail(`VLM returned HTTP ${response.status}`);

let payload;
try {
  payload = await response.json();
} catch (_error) {
  fail("VLM returned invalid JSON");
}
const description = responseText(payload?.choices?.[0]?.message);
if (!description) fail("VLM returned no text description");
process.stdout.write(`${description}\n`);
