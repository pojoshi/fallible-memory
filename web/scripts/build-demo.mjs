// Bundles the REAL engine + agent (src/) into demo/template.html -> demo/index.html.
// The demo therefore runs exactly the code that the tests cover.
import { build } from 'esbuild';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const PLACEHOLDER = '/*__ENGINE_BUNDLE__*/';

const result = await build({
  entryPoints: [join(root, 'src/index.ts')],
  bundle: true,
  write: false,
  format: 'iife',
  globalName: 'MemoryLib',
  target: 'es2019',
  minify: false,
  legalComments: 'none',
});

const code = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const template = readFileSync(join(root, 'demo/template.html'), 'utf8');
if (!template.includes(PLACEHOLDER)) throw new Error('Placeholder missing from demo/template.html');

writeFileSync(join(root, 'demo/index.html'), template.replace(PLACEHOLDER, () => code));
console.log(`demo/index.html built (${Math.round(code.length / 1024)} KB engine bundle inlined)`);
