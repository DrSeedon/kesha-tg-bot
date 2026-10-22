const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const patch = fs.readFileSync(path.join(__dirname, 'websearch-index.patch'), 'utf8');
const indexPatch = patch.split('diff --git a/README.md')[0];
const source = indexPatch.split('\n')
  .filter(line => (line.startsWith('+') && !line.startsWith('+++')) || line.startsWith(' '))
  .map(line => line.slice(1)).join('\n');
const map = source.match(/const MODEL_MAP = \{[\s\S]*?\n\};/)[0];
const resolver = source.match(/function resolveImageModel\(raw\) \{[\s\S]*?\n\}/)[0];
const handler = source.match(/async function handleGenerateImage\(args\) \{[\s\S]*?  const aspectRatio = args.aspect_ratio \|\| '1:1';/)[0];
let fetchCalls = 0;
const sandbox = { fetch: async model => { fetchCalls++; return { model }; } };
vm.createContext(sandbox);
// The omitted handler body is replaced with a fetch spy, so rejection must occur before reaching it.
vm.runInContext(`${map}\n${resolver}\n${handler}\n  return fetch(modelFull);\n}\nglobalThis.resolve = resolveImageModel; globalThis.handle = handleGenerateImage; globalThis.models = MODEL_MAP;`, sandbox);
const canonical = 'google/gemini-nano-banana-2.1';
const error = 'unknown image model. Allowed: nano-banana';
const inputs = [
  ['undefined', undefined, true], ["''", '', true],
  ['nano-banana', 'nano-banana', true], [canonical, canonical, true],
  ...['nano-banana-lite', 'riverflow-fast', 'riverflow-pro', 'riverflow-max', 'seedream', 'flux-klein', 'flux-max', 'gpt-5-image-mini', 'google/gemini-3.1-flash-image-preview', 'google/gemini-3.1-flash-lite-image', 'openai/arbitrary', 'unknown', ' nano-banana ', '__proto__', 'constructor', 'toString'].map(value => [value, value, false]),
  ['null', null, false], ['false', false, false], ['0', 0, false], ['{}', {}, false], ['[]', [], false],
];
(async () => {
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.models)), { 'nano-banana': canonical });
  console.log('| Input | Resolver result | Fetch calls |');
  console.log('|---|---|---|');
  for (const [label, input, allowed] of inputs) {
    const result = JSON.parse(JSON.stringify(sandbox.resolve(input)));
    assert.deepEqual(result, allowed ? { modelFull: canonical } : { error });
    const before = fetchCalls;
    const output = JSON.parse(JSON.stringify(await sandbox.handle({ prompt: 'Draw a tree', model: input })));
    assert.equal(fetchCalls - before, allowed ? 1 : 0);
    assert.deepEqual(output, allowed ? { model: canonical } : { content: [{ type: 'text', text: `Error: ${error}` }] });
    console.log(`| ${label} | ${allowed ? canonical : 'Error: ' + error} | ${fetchCalls - before} |`);
  }
  const before = fetchCalls;
  assert.equal((await sandbox.handle({ prompt: 'Edit a photo', reference_image: '/fixture.png', aspect_ratio: '16:9' })).model, canonical);
  assert.equal(fetchCalls - before, 1);
  console.log(`PASS: ${inputs.length} resolver and handler-guard cases, plus omitted-model edit arguments. The full MCP handler and network are not exercised.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
