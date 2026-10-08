// Validate Mermaid sources with the real Mermaid parser.
//
// stdin:  JSON array of {"id": string, "text": string}
// stdout: JSON object {"<id>": null | "<first line of the parse error>"}
// Exit status 0 when every item was checked (even if some failed to parse),
// 2 on bad input. Mermaid's own logging goes to stderr so stdout stays JSON.
import { JSDOM } from 'jsdom';

const log = (...args) => process.stderr.write(args.map(String).join(' ') + '\n');
console.log = log;
console.info = log;
console.warn = log;
console.debug = log;
console.error = log;

const dom = new JSDOM('<!doctype html><html><body></body></html>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
try {
  globalThis.navigator = dom.window.navigator;
} catch {
  // Node >= 21 defines a read-only navigator; mermaid works with it.
}
globalThis.DOMParser = dom.window.DOMParser;
globalThis.Element = dom.window.Element;

const { default: mermaid } = await import('mermaid');
mermaid.initialize({ startOnLoad: false, logLevel: 'fatal', securityLevel: 'strict' });

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
let items;
try {
  items = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!Array.isArray(items)) throw new Error('expected a JSON array');
} catch (err) {
  log(`mermaid-check: bad input: ${err.message}`);
  process.exit(2);
}

const results = {};
for (const item of items) {
  const id = String(item.id);
  try {
    await mermaid.parse(String(item.text));
    results[id] = null;
  } catch (err) {
    const msg = String((err && (err.message || err.str)) || err);
    results[id] = msg.split('\n').filter((l) => l.trim()).slice(0, 4).join(' | ') || 'parse error';
  }
}
process.stdout.write(JSON.stringify(results));
