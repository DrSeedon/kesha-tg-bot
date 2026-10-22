from pathlib import Path
import difflib
import re

NOTES = Path(__file__).resolve().parent
DESCRIPTION_OLD = "      description: 'Generate or edit an image via OpenRouter. Can create from text prompt, or edit/modify an existing image by providing reference_image path. Cost: ~$0.07/image (nano-banana, default), ~$0.04 (nano-banana-lite).',\n"
DESCRIPTION_NEW = "      description: 'Generate or edit an image via OpenRouter. Can create from text prompt, or edit/modify an existing image by providing reference_image path. Single model: nano-banana (Gemini Nano Banana 2.1), ~$0.034/image at 1K.',\n"
PARAMETER_OLD = """          model: {
            type: 'string',
            description: 'Image model ID. Short aliases: nano-banana (DEFAULT, $0.07/img, Gemini 3.1 Flash Image), nano-banana-lite (cheap $0.04/img, Gemini 3.1 Flash Lite), riverflow-fast, seedream, flux-klein. Or any OpenRouter model ID. Default: nano-banana.'
          },
"""
PARAMETER_NEW = """          model: {
            type: 'string',
            description: 'Optional. Only nano-banana (Gemini Nano Banana 2.1) is allowed; any other value is rejected. Default: nano-banana.'
          },
"""
MAP_OLD = """const MODEL_MAP = {
  'nano-banana': 'google/gemini-3.1-flash-image-preview',
  'nano-banana-lite': 'google/gemini-3.1-flash-lite-image',
  'riverflow-fast': 'sourceful/riverflow-v2-fast',
  'riverflow-pro': 'sourceful/riverflow-v2-pro',
  'riverflow-max': 'sourceful/riverflow-v2-max-preview',
  'flux-klein': 'black-forest-labs/flux-2-klein',
  'flux-max': 'black-forest-labs/flux-2-max',
  'seedream': 'bytedance-seed/seedream-4.5',
};
"""
MAP_NEW = """const MODEL_MAP = {
  'nano-banana': 'google/gemini-nano-banana-2.1',
};
"""
RESOLVER = """function resolveImageModel(raw) {
  const model = raw === undefined || raw === '' ? 'nano-banana' : raw;
  if (model !== 'nano-banana' && model !== MODEL_MAP['nano-banana']) {
    return { error: 'unknown image model. Allowed: nano-banana' };
  }
  return { modelFull: MODEL_MAP['nano-banana'] };
}
"""
SEARCH_OLD = r"""const SEARCH_ALLOWED = new Set(['sonar', 'sonar-pro-search']);

function resolveSearchModel(raw) {
  const short = String(raw || 'sonar').replace(/^perplexity\//, '');
  if (!SEARCH_ALLOWED.has(short)) {
    return { error: `unknown search model. Allowed: sonar, sonar-pro-search` };
  }
  return { short };
}
"""
SEARCH_NEW = SEARCH_OLD.replace('function resolveSearchModel', RESOLVER + '\nfunction resolveSearchModel', 1)
HANDLER_OLD = """async function handleGenerateImage(args) {
  const prompt = args.prompt;
  const modelShort = args.model || 'nano-banana';
  const modelFull = MODEL_MAP[modelShort] || (modelShort.includes('/') ? modelShort : MODEL_MAP['nano-banana']);
  const aspectRatio = args.aspect_ratio || '1:1';
"""
HANDLER_NEW = """async function handleGenerateImage(args) {
  const prompt = args.prompt;
  const resolved = resolveImageModel(args.model);
  if (resolved.error) {
    return { content: [{ type: 'text', text: `Error: ${resolved.error}` }] };
  }
  const modelFull = resolved.modelFull;
  const aspectRatio = args.aspect_ratio || '1:1';
"""
README_OLD = """| `nano-banana` | Дефолт, хорошее качество |
| `nano-banana-lite` | Бюджетный вариант |
| `riverflow-fast` / `riverflow-pro` / `riverflow-max` | Альтернативный движок, от быстрого к качественному |
| `seedream` | Ещё один вариант стиля |
"""
README_NEW = '| `nano-banana` | Gemini Nano Banana 2.1, единственная модель |\n'
IGNORE_OLD = 'node_modules/\ngenerated/\n*.log\n.env\n.env.local\n'
IGNORE_NEW = IGNORE_OLD + 'index.js.bak-*\n'

# Each hunk uses only source lines supplied by the orchestrator.
def hunk(old, new, old_start, delta):
    lines = list(difflib.unified_diff(old.splitlines(True), new.splitlines(True), n=20))[2:]
    header = re.match(r'@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@', lines[0])
    lines[0] = f'@@ -{int(header[1]) + old_start - 1}{header[2] or ""} +{int(header[3]) + old_start - 1 + delta}{header[4] or ""} @@\n'
    return ''.join(lines)

index_chunks = [(69, DESCRIPTION_OLD, DESCRIPTION_NEW), (82, PARAMETER_OLD, PARAMETER_NEW), (102, MAP_OLD, MAP_NEW), (124, SEARCH_OLD, SEARCH_NEW), (248, HANDLER_OLD, HANDLER_NEW)]
patch = 'diff --git a/index.js b/index.js\n--- a/index.js\n+++ b/index.js\n'
delta = 0
for start, old, new in index_chunks:
    patch += hunk(old, new, start, delta)
    delta += len(new.splitlines()) - len(old.splitlines())
for name, old, new, start in [('README.md', README_OLD, README_NEW, 53), ('.gitignore', IGNORE_OLD, IGNORE_NEW, 1)]:
    patch += f'diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n'
    patch += hunk(old, new, start, 0)
(NOTES / 'websearch-index.patch').write_text(patch)
print('Wrote websearch-index.patch. Apply with git apply --unidiff-zero: source context for two hunks was not supplied.')
