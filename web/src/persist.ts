/**
 * persist.ts
 *
 * Node-only file persistence for the engine. The browser demo uses
 * localStorage with the same serialized format (engine.serialize()).
 * Kept out of index.ts so the demo bundle never pulls in node:fs.
 */

import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { MemoryEngine } from './memory-engine';

/** Atomic-ish save: write a temp file, then rename over the target. */
export function saveToFile(engine: MemoryEngine, path: string): void {
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, engine.serialize(), 'utf8');
  renameSync(tmp, path);
}

/** Load a saved engine, or return a fresh one if the file does not exist. */
export function loadFromFile(path: string): MemoryEngine {
  if (!existsSync(path)) return new MemoryEngine();
  return MemoryEngine.deserialize(readFileSync(path, 'utf8'));
}
