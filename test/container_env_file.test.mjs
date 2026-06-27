/**
 * Tests for bin/container.mjs systemd env-file handling.
 *
 * Regression for the June 2026 incident: the KISSKI API key was inlined into the
 * generated systemd unit's ExecStart line. These tests verify that user-provided env
 * (which may contain secrets) is routed through a root-only --env-file instead, so
 * secret VALUES never appear in the unit, and that the env file is mode 0600.
 *
 * Run: node --test test/
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Point the service env dir at a temp location BEFORE importing the module,
// since SERVICE_ENV_DIR is resolved at module load.
const TMP_ENV_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'zr-envdir-'));
process.env.ZOTERO_RAG_ENV_DIR = TMP_ENV_DIR;
process.env.KISSKI_API_KEY = 'super-secret-key-123';

const {
  resolveEnvPairs,
  writeServiceEnvFile,
  buildLegacyUnitContent,
  buildQuadletContent,
} = await import('../bin/container.mjs');

const SECRET = 'super-secret-key-123';

function appCfg(extra = {}) {
  return {
    name: 'zotero-rag',
    imageName: 'localhost/zotero-rag:latest',
    port: 8119,
    env: ['KISSKI_API_KEY', 'RAG_PRESET=remote-kisski'],
    extraEnv: [{ key: 'QDRANT_URL', value: 'http://qdrant:6333' }],
    network: 'zotero-rag-net',
    ...extra,
  };
}

test('resolveEnvPairs resolves name-only from env and passes inline through', () => {
  const pairs = resolveEnvPairs(['KISSKI_API_KEY', 'RAG_PRESET=remote-kisski']);
  assert.deepEqual(pairs, ['KISSKI_API_KEY=super-secret-key-123', 'RAG_PRESET=remote-kisski']);
});

test('resolveEnvPairs skips vars missing from the host environment', () => {
  const pairs = resolveEnvPairs(['DEFINITELY_NOT_SET_VAR_XYZ']);
  assert.deepEqual(pairs, []);
});

test('writeServiceEnvFile writes a 0600 file containing the resolved pairs', () => {
  const p = writeServiceEnvFile('zotero-rag', ['KISSKI_API_KEY', 'RAG_PRESET=remote-kisski']);
  assert.ok(p, 'should return a path');
  const mode = fs.statSync(p).mode & 0o777;
  assert.equal(mode, 0o600, `env file must be 0600, got ${mode.toString(8)}`);
  const content = fs.readFileSync(p, 'utf8');
  assert.match(content, /^KISSKI_API_KEY=super-secret-key-123$/m);
  assert.match(content, /^RAG_PRESET=remote-kisski$/m);
});

test('writeServiceEnvFile returns null when there is nothing to write', () => {
  assert.equal(writeServiceEnvFile('empty-svc', []), null);
  assert.equal(writeServiceEnvFile('empty-svc', undefined), null);
});

test('legacy unit references --env-file and never inlines the secret value', () => {
  const envFile = writeServiceEnvFile('zotero-rag', appCfg().env);
  const unit = buildLegacyUnitContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-qdrant', 'zotero-rag-qdrant', envFile);
  assert.ok(unit.includes(`--env-file ${envFile}`), 'ExecStart must reference the env file');
  assert.ok(!unit.includes(SECRET), 'secret value must NOT appear in the unit');
  // Non-secret internal config is still inlined.
  assert.ok(unit.includes('-e QDRANT_URL=http://qdrant:6333'), 'extraEnv stays inline');
});

test('quadlet unit references EnvironmentFile and never inlines the secret value', () => {
  const envFile = writeServiceEnvFile('zotero-rag', appCfg().env);
  const unit = buildQuadletContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-qdrant', 'zotero-rag-qdrant', envFile);
  assert.ok(unit.includes(`EnvironmentFile=${envFile}`), 'must reference EnvironmentFile=');
  assert.ok(!unit.includes(SECRET), 'secret value must NOT appear in the unit');
  assert.ok(unit.includes('Environment=QDRANT_URL=http://qdrant:6333'), 'extraEnv stays inline');
});

test('units omit env-file directives when there is no env file', () => {
  const cfg = appCfg({ env: [] });
  const legacy = buildLegacyUnitContent(cfg, 'k', 'q', 'q', null);
  const quad = buildQuadletContent(cfg, 'k', 'q', 'q', null);
  assert.ok(!legacy.includes('--env-file'));
  assert.ok(!quad.includes('EnvironmentFile='));
});

test.after(() => {
  fs.rmSync(TMP_ENV_DIR, { recursive: true, force: true });
});
