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
  const unit = buildLegacyUnitContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-kreuzberg-container', 'zotero-rag-qdrant', 'zotero-rag-qdrant', envFile);
  assert.ok(unit.includes(`--env-file ${envFile}`), 'ExecStart must reference the env file');
  assert.ok(!unit.includes(SECRET), 'secret value must NOT appear in the unit');
  // Non-secret internal config is still inlined.
  assert.ok(unit.includes('-e QDRANT_URL=http://qdrant:6333'), 'extraEnv stays inline');
});

test('quadlet unit references EnvironmentFile and never inlines the secret value', () => {
  const envFile = writeServiceEnvFile('zotero-rag', appCfg().env);
  const unit = buildQuadletContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-kreuzberg-container', 'zotero-rag-qdrant', 'zotero-rag-qdrant', envFile);
  assert.ok(unit.includes(`EnvironmentFile=${envFile}`), 'must reference EnvironmentFile=');
  assert.ok(!unit.includes(SECRET), 'secret value must NOT appear in the unit');
  assert.ok(unit.includes('Environment=QDRANT_URL=http://qdrant:6333'), 'extraEnv stays inline');
});

test('units omit env-file directives when there is no env file', () => {
  const cfg = appCfg({ env: [] });
  const legacy = buildLegacyUnitContent(cfg, 'k', 'kc', 'q', 'q', null);
  const quad = buildQuadletContent(cfg, 'k', 'kc', 'q', 'q', null);
  assert.ok(!legacy.includes('--env-file'));
  assert.ok(!quad.includes('EnvironmentFile='));
});

test('main unit removes the kreuzberg container directly, never via systemctl restart on its own Requires= dependency', () => {
  // Regression: `ExecStartPre=systemctl restart <kreuzberg>.service` on a unit that
  // itself Requires=/After= that same kreuzberg unit creates a job-transaction
  // conflict — systemd kills the ExecStartPre (SIGTERM) and the whole unit
  // crash-loops. Removing the container directly lets kreuzberg's own
  // Restart=always bring it back independently, with no competing job.
  const legacy = buildLegacyUnitContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-kreuzberg-container', 'zotero-rag-qdrant', 'zotero-rag-qdrant', null);
  const quad = buildQuadletContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-kreuzberg-container', 'zotero-rag-qdrant', 'zotero-rag-qdrant', null);
  for (const unit of [legacy, quad]) {
    assert.ok(!/systemctl restart .*kreuzberg/.test(unit), 'must not systemctl-restart the kreuzberg service from its own dependent unit');
    assert.ok(unit.includes('ExecStartPre=-/usr/bin/podman rm -f zotero-rag-kreuzberg-container'), 'must remove the kreuzberg container directly');
  }
});

test('main unit depends on kreuzberg via Wants=, not Requires=, to avoid stop propagation', () => {
  // Regression: even with the raw `podman rm -f` ExecStartPre above (no systemctl
  // job involved), a hard Requires=<kreuzberg>.service on this unit still
  // crash-loops it — systemd propagates kreuzberg's own stop/restart cycle
  // (triggered by having its container ripped out) back onto any unit that
  // Requires= it. Qdrant has no such ExecStartPre disruption and keeps Requires=
  // since its own readiness probe below is a genuine hard dependency.
  const legacy = buildLegacyUnitContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-kreuzberg-container', 'zotero-rag-qdrant', 'zotero-rag-qdrant', null);
  const quad = buildQuadletContent(appCfg(), 'zotero-rag-kreuzberg', 'zotero-rag-kreuzberg-container', 'zotero-rag-qdrant', 'zotero-rag-qdrant', null);
  for (const unit of [legacy, quad]) {
    assert.ok(unit.includes('Wants=zotero-rag-kreuzberg.service'), 'kreuzberg must be a soft (Wants=) dependency');
    assert.ok(!unit.includes('Requires=zotero-rag-kreuzberg.service'), 'kreuzberg must NOT be a hard (Requires=) dependency');
    assert.ok(unit.includes('Requires=zotero-rag-qdrant.service'), 'qdrant must remain a hard (Requires=) dependency');
  }
});

test.after(() => {
  fs.rmSync(TMP_ENV_DIR, { recursive: true, force: true });
});
