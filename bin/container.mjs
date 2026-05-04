#!/usr/bin/env node

/**
 * Container Management Script for Zotero RAG
 *
 * Manages both the Zotero RAG backend container and the kreuzberg document
 * extraction sidecar.  The two containers communicate over a shared Docker
 * bridge network.
 *
 * For multi-container local deployments, prefer docker-compose:
 *   docker compose up -d
 *
 * Use this script for server deployments and CI.
 *
 * ---------------------------------------------------------------------------
 * External dependencies
 * ---------------------------------------------------------------------------
 *
 * All subcommands:
 *   - docker (20.10+) OR podman (4.0+) must be installed and the daemon/socket
 *     must be reachable.  The script auto-detects which one is available.
 *
 * `deploy` with nginx/SSL (default):
 *   - nginx          — reverse proxy; managed via sites-available/sites-enabled
 *   - certbot        — Let's Encrypt SSL; requires the nginx plugin
 *                      (e.g. `apt install certbot python3-certbot-nginx`)
 *   - systemctl      — used to reload/restart nginx
 *   All three require root (sudo).  Pass --no-nginx --no-ssl to skip them.
 *
 * `deploy --systemd-service`:
 *   - systemctl      — enables and starts the generated units
 *   - Podman 4.4+    — Quadlet generator in /usr/lib/systemd/system-generators/
 *                      is used when present; older Podman falls back to a
 *                      traditional [Service] unit written to /etc/systemd/system/
 *   Requires root (sudo).
 *
 * Network backend (podman only):
 *   - netavark (Podman 4.0+ default) OR cni-plugins (legacy).
 *     Both call iptables internally.  On Debian/Ubuntu, iptables lives in
 *     /usr/sbin, which sudo strips from PATH — always run with:
 *       sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy ...
 *
 * Tested on:
 *   - Debian 12 (Bookworm) with Podman 4.3.1  [legacy unit fallback]
 *   - Ubuntu 22.04 / 24.04 with Docker 24+
 *   - macOS (Docker Desktop) — nginx/SSL/systemd subcommands not available
 *   - Windows — deploy subcommand not supported
 */

import { execSync, spawn } from 'child_process';
import fs from 'fs';
import readline from 'readline';
import { Command } from 'commander';

// ============================================================================
// Configuration
// ============================================================================

const APP_NAME = 'zotero-rag';
const REGISTRY = 'docker.io/cboulanger/zotero-rag';
const KREUZBERG_IMAGE = 'ghcr.io/kreuzberg-dev/kreuzberg:latest';
const KREUZBERG_PORT = 8000;
const QDRANT_IMAGE = 'docker.io/qdrant/qdrant:v1.15';
const QDRANT_PORT = 6333;
const DEFAULT_PORT = 8119;
const CONTAINER_PORT = 8119;
const DEFAULT_ZOTERO_HOST = 'http://host.docker.internal:23119';
const NETWORK_NAME = `${APP_NAME}-net`;

/** @type {string|null} */
let containerCmd = null;

/** @type {{username?: string, token?: string}} */
let credentials = {};

// ============================================================================
// Utility Functions
// ============================================================================

/**
 * Check whether a container tool binary exists AND its daemon/socket is reachable
 * @param {string} cmd
 * @returns {boolean}
 */
function isToolUsable(cmd) {
  try {
    execSync(`${cmd} --version`, { stdio: 'ignore' });
  } catch {
    return false;
  }
  try {
    execSync(`${cmd} info`, { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

/**
 * Detect container tool (docker or podman, prefer docker).
 */
function detectContainerTool() {
  for (const cmd of ['docker', 'podman']) {
    if (isToolUsable(cmd)) {
      containerCmd = cmd;
      console.log(`[INFO] Using ${cmd} as container tool`);
      return;
    }
  }
  console.log('[ERROR] No usable container runtime found.');
  console.log('[INFO] Make sure Docker Desktop (or the Docker daemon) is running,');
  console.log('[INFO] or start a Podman machine with: podman machine start');
  process.exit(1);
}

/**
 * Load environment variables from .env file (does not override existing env)
 */
function loadEnv() {
  const envPath = '.env';
  if (!fs.existsSync(envPath)) return;
  const envContent = fs.readFileSync(envPath, 'utf8');
  for (let line of envContent.split('\n')) {
    line = line.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    const value = line.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
    if (key && !process.env[key]) process.env[key] = value;
  }
}

/**
 * Validate required env vars for push/registry operations
 */
function validateRegistryEnv() {
  loadEnv();
  const missing = ['DOCKER_HUB_USERNAME', 'DOCKER_HUB_TOKEN'].filter(v => !process.env[v]);
  if (missing.length > 0) {
    console.log(`[ERROR] Missing required environment variables: ${missing.join(', ')}`);
    console.log('[INFO] Add them to your .env file or export them before running this command.');
    process.exit(1);
  }
  credentials.username = process.env.DOCKER_HUB_USERNAME;
  credentials.token = process.env.DOCKER_HUB_TOKEN;
}

/**
 * Resolve image tag: use provided value, or derive from git, or fall back to 'latest'
 * @param {string|undefined} provided
 * @returns {string}
 */
function resolveTag(provided) {
  if (provided) return provided;
  try {
    execSync('git rev-parse --git-dir', { stdio: 'ignore' });
    const branch = execSync('git rev-parse --abbrev-ref HEAD', { encoding: 'utf8' }).trim();
    if (branch === 'main' || branch === 'master') return 'latest';
    const hash = execSync('git rev-parse --short HEAD', { encoding: 'utf8' }).trim();
    return `${branch}-${hash}`;
  } catch {
    return 'latest';
  }
}

/**
 * Execute a command with live stdio, returning a Promise
 * @param {string} cmd
 * @param {string[]} args
 * @param {{silent?: boolean}} [opts]
 * @returns {Promise<void>}
 */
function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: opts.silent ? 'ignore' : 'inherit' });
    child.on('close', code => code === 0 ? resolve() : reject(new Error(`Exit code ${code}`)));
    child.on('error', reject);
  });
}

/**
 * Prompt for y/N confirmation
 * @param {string} question
 * @returns {Promise<boolean>}
 */
function confirm(question) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(resolve => {
    rl.question(question, answer => { rl.close(); resolve(answer.toLowerCase().startsWith('y')); });
  });
}

// ============================================================================
// Network Management
// ============================================================================

/**
 * Ensure the shared Docker network exists; create it if absent.
 * @param {string} name
 */
function ensureNetwork(name) {
  try {
    execSync(`${containerCmd} network inspect ${name}`, { stdio: 'ignore' });
    return; // already exists
  } catch {}
  try {
    execSync(`${containerCmd} network create ${name}`, { stdio: 'inherit' });
    console.log(`[INFO] Created network ${name}`);
  } catch (e) {
    console.log(`[ERROR] Failed to create network ${name}: ${e.message}`);
    process.exit(1);
  }
}

/**
 * Remove the shared Docker network (only if no containers are attached).
 * @param {string} name
 */
function removeNetwork(name) {
  try {
    execSync(`${containerCmd} network rm ${name}`, { stdio: 'ignore' });
    console.log(`[INFO] Removed network ${name}`);
  } catch {
    // May still have containers attached — ignore
  }
}

// ============================================================================
// Kreuzberg Sidecar Management
// ============================================================================

/**
 * Start the kreuzberg sidecar container.
 * @param {string} kreuzbergName  Container name for the sidecar
 * @param {string} networkName    Docker network to attach to
 * @returns {Promise<void>}
 */
async function startKreuzberg(kreuzbergName, networkName) {
  // Stop+remove any existing sidecar with this name
  stopExisting(kreuzbergName);

  console.log(`[INFO] Starting kreuzberg sidecar (${kreuzbergName})...`);

  // Pull latest kreuzberg image
  try {
    execSync(`${containerCmd} pull ${KREUZBERG_IMAGE}`, { stdio: 'inherit' });
  } catch (e) {
    console.log(`[WARNING] Could not pull ${KREUZBERG_IMAGE}: ${e.message}`);
    console.log('[INFO] Continuing with existing image if available...');
  }

  const kreuzbergMemory = process.env.KREUZBERG_MEMORY_LIMIT || '8g';
  const args = [
    'run', '-d',
    '--name', kreuzbergName,
    '--network', networkName,
    '--network-alias', 'kreuzberg',
    '--restart', 'unless-stopped',
    '--memory', kreuzbergMemory,
    KREUZBERG_IMAGE,
  ];

  return new Promise((resolve, reject) => {
    const child = spawn(containerCmd, args, { stdio: 'pipe' });
    let out = '';
    let err = '';
    if (child.stdout) child.stdout.on('data', d => { out += d.toString(); });
    if (child.stderr) child.stderr.on('data', d => { err += d.toString(); process.stderr.write(d); });
    child.on('close', code => {
      if (code !== 0) return reject(new Error(`kreuzberg sidecar start failed (exit ${code}):\n${err}`));
      console.log(`[SUCCESS] kreuzberg sidecar started (${out.trim().slice(0, 12)})`);
      resolve();
    });
    child.on('error', reject);
  });
}

/**
 * Start the Qdrant vector database sidecar container.
 * @param {string} qdrantName   Container name for the sidecar
 * @param {string} networkName  Docker network to attach to
 * @param {string} [storagePath] Host path to mount at /qdrant/storage for persistence
 * @returns {Promise<void>}
 */
async function startQdrant(qdrantName, networkName, storagePath) {
  stopExisting(qdrantName);

  console.log(`[INFO] Starting Qdrant sidecar (${qdrantName})...`);
  if (!storagePath) {
    console.log('[WARNING] No Qdrant storage path configured — data will not be persisted across restarts.');
    console.log('[INFO] Use --qdrant-data-dir <path> or --data-dir <path> to enable persistence.');
  }

  try {
    execSync(`${containerCmd} pull ${QDRANT_IMAGE}`, { stdio: 'inherit' });
  } catch (e) {
    console.log(`[WARNING] Could not pull ${QDRANT_IMAGE}: ${e.message}`);
    console.log('[INFO] Continuing with existing image if available...');
  }

  const args = [
    'run', '-d',
    '--name', qdrantName,
    '--network', networkName,
    '--network-alias', 'qdrant',
    '--restart', 'unless-stopped',
  ];

  if (storagePath) {
    if (!fs.existsSync(storagePath)) fs.mkdirSync(storagePath, { recursive: true });
    args.push('-v', `${storagePath}:/qdrant/storage`);
  }

  args.push(QDRANT_IMAGE);

  return new Promise((resolve, reject) => {
    const child = spawn(containerCmd, args, { stdio: 'pipe' });
    let out = '';
    let err = '';
    if (child.stdout) child.stdout.on('data', d => { out += d.toString(); });
    if (child.stderr) child.stderr.on('data', d => { err += d.toString(); process.stderr.write(d); });
    child.on('close', code => {
      if (code !== 0) return reject(new Error(`Qdrant sidecar start failed (exit ${code}):\n${err}`));
      console.log(`[SUCCESS] Qdrant sidecar started (${out.trim().slice(0, 12)})`);
      resolve();
    });
    child.on('error', reject);
  });
}

/**
 * Stop + optionally remove the Qdrant sidecar.
 * @param {string} qdrantName
 * @param {boolean} [remove]
 */
function stopQdrant(qdrantName, remove = true) {
  try {
    const id = execSync(
      `${containerCmd} ps -a --filter "name=^${qdrantName}$" --format "{{.ID}}"`,
      { encoding: 'utf8', stdio: 'pipe' }
    ).trim();
    if (!id) return;
    execSync(`${containerCmd} stop ${qdrantName}`, { stdio: 'inherit' });
    if (remove) execSync(`${containerCmd} rm ${qdrantName}`, { stdio: 'inherit' });
    console.log(`[INFO] Stopped Qdrant sidecar (${qdrantName})`);
  } catch {}
}

/**
 * Wait until Qdrant is accepting connections on port 6333.
 * Uses /proc/net/tcp inside the container — works on any Linux image without
 * requiring curl/wget/nc to be installed.  Port 6333 = 0x18BD.
 * Throws if Qdrant does not become ready within the timeout.
 * @param {string} qdrantName - Qdrant container name
 * @param {number} [timeoutMs] - Max wait in ms (default: 180000)
 */
async function waitForQdrant(qdrantName, timeoutMs = 180000) {
  const interval = 2000;
  const deadline = Date.now() + timeoutMs;
  console.log(`[INFO] Waiting for Qdrant (${qdrantName}) to accept connections...`);
  while (Date.now() < deadline) {
    try {
      execSync(
        `${containerCmd} exec ${qdrantName} grep -q ":18BD " /proc/net/tcp`,
        { stdio: 'ignore' }
      );
      console.log('[INFO] Qdrant is ready');
      return;
    } catch {}
    await new Promise(r => setTimeout(r, interval));
  }
  throw new Error(`Qdrant (${qdrantName}) did not become ready within ${timeoutMs / 1000}s`);
}

/**
 * Stop + optionally remove the kreuzberg sidecar.
 * @param {string} kreuzbergName
 * @param {boolean} [remove]
 */
function stopKreuzberg(kreuzbergName, remove = true) {
  try {
    const id = execSync(
      `${containerCmd} ps -a --filter "name=^${kreuzbergName}$" --format "{{.ID}}"`,
      { encoding: 'utf8', stdio: 'pipe' }
    ).trim();
    if (!id) return;
    execSync(`${containerCmd} stop ${kreuzbergName}`, { stdio: 'inherit' });
    if (remove) execSync(`${containerCmd} rm ${kreuzbergName}`, { stdio: 'inherit' });
    console.log(`[INFO] Stopped kreuzberg sidecar (${kreuzbergName})`);
  } catch {}
}

// ============================================================================
// Build
// ============================================================================

/**
 * @param {string} tag
 * @param {boolean} noCache
 * @param {boolean} installLocalModels
 * @param {string|undefined} platform
 * @returns {Promise<boolean>}
 */
async function buildImage(tag, noCache, installLocalModels = false, platform = undefined) {
  const fullTag = `${APP_NAME}:${tag}`;
  console.log(
    `[INFO] Building ${fullTag} (local-models: ${installLocalModels ? 'included' : 'skipped'}` +
    `${platform ? `, platform: ${platform}` : ''})...`
  );
  if (installLocalModels) {
    console.log('[INFO] --local-models: sentence-transformers/torch will be installed (~1-2 GB extra)');
  }
  console.log('[INFO] OCR is handled by the kreuzberg sidecar container — no Tesseract in main image');

  const args = ['build'];
  if (noCache) args.push('--no-cache');
  if (platform) args.push('--platform', platform);
  args.push('--build-arg', `INSTALL_LOCAL_MODELS=${installLocalModels}`);
  args.push('-t', fullTag);
  if (tag !== 'latest') args.push('-t', `${APP_NAME}:latest`);
  args.push('.');

  try {
    await run(containerCmd, args);
    console.log('[SUCCESS] Image built successfully');
    return true;
  } catch (e) {
    console.log('[ERROR] Build failed:', e.message);
    return false;
  }
}

/**
 * @param {{tag?: string, cache?: boolean, localModels?: boolean, platform?: string, yes?: boolean}} options
 */
async function handleBuild(options) {
  console.log('Zotero RAG - Container Build');
  console.log('=============================');
  const tag = resolveTag(options.tag);
  const installLocalModels = options.localModels === true;
  console.log(`[INFO] Tag: ${tag}  local-models: ${installLocalModels}`);
  if (!options.yes && !(await confirm('Continue with build? (y/N): '))) process.exit(0);
  if (!(await buildImage(tag, options.cache === false, installLocalModels, options.platform))) process.exit(1);
  console.log(`[INFO] To push: node bin/container.mjs push --tag ${tag}`);
}

// ============================================================================
// Push
// ============================================================================

/**
 * @param {string} tag
 */
function tagForRegistry(tag) {
  const local = `${APP_NAME}:${tag}`;
  const remote = `${credentials.username}/${APP_NAME}:${tag}`;
  execSync(`${containerCmd} tag ${local} ${remote}`, { stdio: 'inherit' });
  if (tag !== 'latest') {
    try { execSync(`${containerCmd} tag ${APP_NAME}:latest ${credentials.username}/${APP_NAME}:latest`, { stdio: 'inherit' }); }
    catch { /* latest may not exist */ }
  }
}

/**
 * @returns {Promise<boolean>}
 */
async function registryLogin() {
  console.log(`[INFO] Logging in as ${credentials.username}...`);
  return new Promise((resolve) => {
    const child = spawn(containerCmd, ['login', '--username', credentials.username, '--password-stdin', 'docker.io'], {
      stdio: ['pipe', 'inherit', 'inherit']
    });
    child.stdin.write(credentials.token);
    child.stdin.end();
    child.on('close', code => {
      if (code === 0) { console.log('[SUCCESS] Logged in'); resolve(true); }
      else { console.log('[ERROR] Login failed'); resolve(false); }
    });
  });
}

/**
 * @param {string} tag
 * @returns {Promise<boolean>}
 */
async function pushImage(tag) {
  const remote = `${credentials.username}/${APP_NAME}`;
  try {
    await run(containerCmd, ['push', `${remote}:${tag}`]);
    if (tag !== 'latest') {
      try { await run(containerCmd, ['push', `${remote}:latest`]); }
      catch { console.log('[WARNING] Could not push latest tag'); }
    }
    console.log('[SUCCESS] Push complete');
    return true;
  } catch (e) {
    console.log('[ERROR] Push failed:', e.message);
    return false;
  }
}

/**
 * @param {{tag?: string, build?: boolean, cache?: boolean, localModels?: boolean, platform?: string, yes?: boolean}} options
 */
async function handlePush(options) {
  console.log('Zotero RAG - Container Push');
  console.log('============================');
  validateRegistryEnv();
  const tag = resolveTag(options.tag);
  const doBuild = options.build !== false;
  const installLocalModels = options.localModels === true;
  console.log(`[INFO] Tag: ${tag}  Registry: ${credentials.username}/${APP_NAME}  Build: ${doBuild}  local-models: ${installLocalModels}`);
  if (!options.yes && !(await confirm(`Continue with ${doBuild ? 'build + ' : ''}push? (y/N): `))) process.exit(0);

  process.on('exit', () => { try { execSync(`${containerCmd} logout docker.io`, { stdio: 'ignore' }); } catch {} });

  if (doBuild) {
    if (!(await buildImage(tag, options.cache === false, installLocalModels, options.platform))) process.exit(1);
  }
  tagForRegistry(tag);
  if (!(await registryLogin())) process.exit(1);
  if (!(await pushImage(tag))) process.exit(1);
}

// ============================================================================
// Start
// ============================================================================

/**
 * Resolve the host path for Qdrant persistent storage.
 * Explicit --qdrant-data-dir wins; falls back to <dataDir>/qdrant-server; undefined = no persistence.
 * @param {string|undefined} qdrantDataDir
 * @param {string|undefined} dataDir
 * @returns {string|undefined}
 */
function resolveQdrantStoragePath(qdrantDataDir, dataDir) {
  if (qdrantDataDir) return qdrantDataDir;
  if (dataDir) return `${dataDir}/qdrant-server`;
  return undefined;
}

/**
 * Add --env flags to runArgs from an array of "KEY" or "KEY=VAL" specs
 * @param {string[]} runArgs
 * @param {string[]|undefined} envSpecs
 */
function addEnvArgs(runArgs, envSpecs) {
  if (!envSpecs) return;
  for (const spec of envSpecs) {
    if (spec.includes('=')) {
      runArgs.push('-e', spec);
    } else {
      const val = process.env[spec];
      if (val !== undefined) runArgs.push('-e', `${spec}=${val}`);
      else console.log(`[WARNING] Env var ${spec} not found in host environment, skipping`);
    }
  }
}

/**
 * Core container start logic
 * @param {{
 *   name: string,
 *   imageName: string,
 *   port: number,
 *   detach?: boolean,
 *   restart?: string,
 *   env?: string[],
 *   volumes?: Array<{host: string, container: string}>,
 *   extraEnv?: Array<{key: string, value: string}>,
 *   addHost?: boolean,
 *   network?: string,
 * }} cfg
 */
async function startContainer(cfg) {
  const { name, imageName, port, detach = true, restart, env, volumes = [], extraEnv = [], addHost, network } = cfg;

  const args = ['run', detach ? '-d' : '', '--name', name, '-p', `${port}:${CONTAINER_PORT}`].filter(Boolean);

  if (restart) args.push('--restart', restart);
  if (addHost) {
    // host-gateway requires podman 4.0+ / docker 20.10+; resolve the host IP directly for compatibility
    let hostIp = 'host-gateway';
    try { hostIp = execSync("hostname -I | awk '{print $1}'", { encoding: 'utf8' }).trim(); } catch {}
    args.push(`--add-host=host.docker.internal:${hostIp}`);
  }
  if (network) args.push('--network', network);

  for (const { key, value } of extraEnv) args.push('-e', `${key}=${value}`);
  addEnvArgs(args, env);

  for (const v of volumes) {
    if (!fs.existsSync(v.host)) fs.mkdirSync(v.host, { recursive: true });
    args.push('-v', `${v.host}:${v.container}`);
  }
  args.push(imageName);

  console.log(`[INFO] ${containerCmd} ${args.join(' ')}`);

  return new Promise((resolve, reject) => {
    const child = spawn(containerCmd, args, { stdio: detach ? 'pipe' : 'inherit' });
    let out = '';
    if (detach && child.stdout) child.stdout.on('data', d => { out += d.toString(); });
    child.on('close', code => {
      if (code !== 0) return reject(new Error(`Exit code ${code}`));
      if (detach) console.log(`[SUCCESS] Container started: ${name} (${out.trim().slice(0, 12)})`);
      resolve();
    });
    child.on('error', reject);
  });
}

/**
 * Resolve which image to use: local, then registry, then pull
 * @param {string} tag
 * @returns {string}
 */
function resolveImage(tag) {
  const local = `${APP_NAME}:${tag}`;
  const remote = `${REGISTRY}:${tag}`;

  try { execSync(`${containerCmd} image inspect ${local}`, { stdio: 'ignore' }); return local; } catch {}
  try { execSync(`${containerCmd} image inspect ${remote}`, { stdio: 'ignore' }); return remote; } catch {}

  console.log(`[INFO] No local image found, pulling ${remote}...`);
  execSync(`${containerCmd} pull ${remote}`, { stdio: 'inherit' });
  return remote;
}

/**
 * Stop + remove a container if it exists
 * @param {string} name
 */
function stopExisting(name) {
  try {
    const id = execSync(`${containerCmd} ps -a --filter "name=^${name}$" --format "{{.ID}}"`, { encoding: 'utf8', stdio: 'pipe' }).trim();
    if (id) {
      execSync(`${containerCmd} stop ${name}`, { stdio: 'inherit' });
      execSync(`${containerCmd} rm -f ${name}`, { stdio: 'inherit' });
    }
  } catch {}
}

/**
 * Return true if a container is currently running (not stopped/exited).
 * @param {string} name
 * @returns {boolean}
 */
function isContainerRunning(name) {
  try {
    const id = execSync(
      `${containerCmd} ps --filter "name=^${name}$" --format "{{.ID}}"`,
      { encoding: 'utf8', stdio: 'pipe' }
    ).trim();
    return !!id;
  } catch {
    return false;
  }
}

/**
 * @param {{
 *   tag?: string, name?: string, port?: number, detach?: boolean,
 *   dataDir?: string, qdrantDataDir?: string, zoteroHost?: string, env?: string[],
 *   volume?: string[], restart?: string, noKreuzberg?: boolean, noQdrant?: boolean
 * }} options
 */
async function handleStart(options) {
  console.log('Zotero RAG - Container Start');
  console.log('=============================');
  const tag = options.tag || 'latest';
  const name = options.name || `${APP_NAME}-${tag}`;
  const port = options.port || DEFAULT_PORT;
  const detach = options.detach !== false;
  const kreuzbergName = `${name}-kreuzberg`;
  const qdrantName = `${name}-qdrant`;
  const qdrantStoragePath = resolveQdrantStoragePath(options.qdrantDataDir, options.dataDir);

  const imageName = resolveImage(tag);
  stopExisting(name);

  const useNetwork = !options.noKreuzberg || !options.noQdrant;

  // Set up shared network and sidecars
  if (useNetwork) ensureNetwork(NETWORK_NAME);
  if (!options.noKreuzberg) await startKreuzberg(kreuzbergName, NETWORK_NAME);
  if (!options.noQdrant) {
    await startQdrant(qdrantName, NETWORK_NAME, qdrantStoragePath);
    await waitForQdrant(qdrantName);
  }

  const volumes = [];
  const extraEnv = [];

  if (options.dataDir) {
    volumes.push({ host: options.dataDir, container: '/data' });
    extraEnv.push({ key: 'DATA_PATH', value: '/data' });
  }

  if (options.volume) {
    for (const spec of options.volume) {
      const [host, container] = spec.split(':');
      if (!host || !container) { console.error(`[ERROR] Invalid volume: ${spec}`); process.exit(1); }
      volumes.push({ host, container });
    }
  }

  const zoteroHost = options.zoteroHost || DEFAULT_ZOTERO_HOST;
  extraEnv.push({ key: 'KREUZBERG_URL', value: `http://kreuzberg:${KREUZBERG_PORT}` });
  extraEnv.push({ key: 'QDRANT_URL', value: `http://qdrant:${QDRANT_PORT}` });

  const addHost = process.platform === 'linux';

  console.log(`[INFO] name=${name} image=${imageName} port=${port} zotero=${zoteroHost}`);

  try {
    await startContainer({
      name, imageName, port, detach,
      restart: options.restart,
      env: options.env,
      volumes, extraEnv, addHost,
      network: useNetwork ? NETWORK_NAME : undefined,
    });
    if (detach) {
      console.log(`\n[INFO] Logs:  ${containerCmd} logs -f ${name}`);
      console.log(`[INFO] Stop:  node bin/container.mjs stop --name ${name}`);
    }
  } catch (e) {
    console.error('[ERROR] Failed to start container:', e.message);
    process.exit(1);
  }
}

// ============================================================================
// Stop
// ============================================================================

/**
 * @param {{name?: string, all?: boolean, remove?: boolean}} options
 */
async function handleStop(options) {
  console.log('Zotero RAG - Container Stop');
  console.log('============================');

  if (options.all) {
    const lines = execSync(`${containerCmd} ps -a --filter "name=${APP_NAME}" --format "{{.ID}} {{.Names}}"`, { encoding: 'utf8', stdio: 'pipe' }).trim();
    if (!lines) { console.log('[INFO] No containers found'); return; }
    for (const line of lines.split('\n')) {
      const [id, cname] = line.split(' ');
      console.log(`[INFO] Stopping ${cname}...`);
      try {
        execSync(`${containerCmd} stop ${id}`, { stdio: 'inherit' });
        if (options.remove) execSync(`${containerCmd} rm ${id}`, { stdio: 'inherit' });
      } catch (e) { console.error(`[ERROR] ${e.message}`); }
    }
    if (options.remove) removeNetwork(NETWORK_NAME);
    return;
  }

  const name = options.name || `${APP_NAME}-latest`;
  const id = execSync(`${containerCmd} ps -a --filter "name=^${name}$" --format "{{.ID}}"`, { encoding: 'utf8', stdio: 'pipe' }).trim();
  if (!id) { console.error(`[ERROR] Container '${name}' not found`); process.exit(1); }
  execSync(`${containerCmd} stop ${name}`, { stdio: 'inherit' });
  if (options.remove) execSync(`${containerCmd} rm ${name}`, { stdio: 'inherit' });

  // Stop the associated sidecars
  stopKreuzberg(`${name}-kreuzberg`, options.remove !== false);
  stopQdrant(`${name}-qdrant`, options.remove !== false);
  if (options.remove) removeNetwork(NETWORK_NAME);

  console.log('[SUCCESS] Done');
}

// ============================================================================
// Restart
// ============================================================================

/**
 * @param {{name?: string, tag?: string, port?: number, dataDir?: string, zoteroHost?: string, env?: string[], volume?: string[], restart?: string}} options
 */
async function handleRestart(options) {
  console.log('Zotero RAG - Container Restart');
  console.log('===============================');
  const name = options.name || `${APP_NAME}-latest`;

  try {
    const id = execSync(`${containerCmd} ps -a --filter "name=^${name}$" --format "{{.ID}}"`, { encoding: 'utf8', stdio: 'pipe' }).trim();
    if (id) {
      execSync(`${containerCmd} stop ${name}`, { stdio: 'inherit' });
      // Restart sidecars too
      const kreuzbergName = `${name}-kreuzberg`;
      const qdrantName = `${name}-qdrant`;
      const kreuzbergId = execSync(
        `${containerCmd} ps -a --filter "name=^${kreuzbergName}$" --format "{{.ID}}"`,
        { encoding: 'utf8', stdio: 'pipe' }
      ).trim();
      const qdrantId = execSync(
        `${containerCmd} ps -a --filter "name=^${qdrantName}$" --format "{{.ID}}"`,
        { encoding: 'utf8', stdio: 'pipe' }
      ).trim();
      if (kreuzbergId) execSync(`${containerCmd} stop ${kreuzbergName}`, { stdio: 'inherit' });
      if (qdrantId) execSync(`${containerCmd} stop ${qdrantName}`, { stdio: 'inherit' });

      // Pull + recreate kreuzberg so the restart picks up any new image
      if (kreuzbergId) await startKreuzberg(kreuzbergName, NETWORK_NAME);
      if (qdrantId) {
        execSync(`${containerCmd} start ${qdrantName}`, { stdio: 'inherit' });
        await waitForQdrant(qdrantName);
      }
      execSync(`${containerCmd} start ${name}`, { stdio: 'inherit' });
      console.log(`[SUCCESS] Restarted ${name} + ${kreuzbergName} + ${qdrantName}`);
      console.log(`[INFO] Logs: ${containerCmd} logs -f ${name}`);
    } else {
      console.log(`[INFO] Container '${name}' not found, creating new container...`);
      await handleStart(options);
    }
  } catch (e) {
    console.error('[ERROR]', e.message);
    process.exit(1);
  }
}

// ============================================================================
// Logs
// ============================================================================

/**
 * @param {{name?: string, follow?: boolean, tail?: number, kreuzberg?: boolean, qdrant?: boolean}} options
 */
async function handleLogs(options) {
  const baseName = options.name || `${APP_NAME}-latest`;
  const name = options.kreuzberg
    ? `${baseName}-kreuzberg`
    : options.qdrant
      ? `${baseName}-qdrant`
      : baseName;
  const args = ['logs'];
  if (options.follow) args.push('-f');
  if (options.tail !== undefined) args.push('--tail', String(options.tail));
  args.push(name);
  try {
    execSync(`${containerCmd} ${args.join(' ')}`, { stdio: 'inherit' });
  } catch (e) {
    console.error('[ERROR]', e.message);
    process.exit(1);
  }
}

// ============================================================================
// Deploy
// ============================================================================

/**
 * Return true if an nginx site config already exists for this FQDN.
 * @param {string} fqdn
 * @returns {boolean}
 */
function isNginxConfigured(fqdn) {
  return fs.existsSync(`/etc/nginx/sites-available/${APP_NAME}-${fqdn}`);
}

/**
 * Return true if a Let's Encrypt certificate already exists for this FQDN.
 * @param {string} fqdn
 * @returns {boolean}
 */
function isSSLConfigured(fqdn) {
  return fs.existsSync(`/etc/letsencrypt/live/${fqdn}/fullchain.pem`);
}

/**
 * @param {string} fqdn
 * @param {number} port
 * @param {string} [maxBodySize]
 * @returns {boolean}
 */
function setupNginx(fqdn, port, maxBodySize = '300M') {
  console.log('[INFO] Setting up nginx...');
  const config = `# Zotero RAG configuration for ${fqdn}
server {
    server_name ${fqdn};

    location / {
        proxy_pass http://127.0.0.1:${port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_redirect off;
        client_max_body_size ${maxBodySize};
        proxy_read_timeout 300s;
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
    }

    # Document indexing — OCR + embedding can take many minutes for large PDFs
    location /api/index/document {
        proxy_pass http://127.0.0.1:${port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_redirect off;
        client_max_body_size ${maxBodySize};
        proxy_read_timeout 1800s;
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
    }

    # SSE endpoints - disable buffering
    location /api/query/stream {
        proxy_pass http://127.0.0.1:${port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }

    listen 80;
}
`;
  const configFile = `/etc/nginx/sites-available/${APP_NAME}-${fqdn}`;
  const enabledLink = `/etc/nginx/sites-enabled/${APP_NAME}-${fqdn}`;
  try {
    fs.writeFileSync(configFile, config);
    if (fs.existsSync(enabledLink)) fs.unlinkSync(enabledLink);
    fs.symlinkSync(configFile, enabledLink);
    execSync('nginx -t', { stdio: 'inherit' });
    try { execSync('systemctl reload nginx', { stdio: 'inherit' }); }
    catch { execSync('systemctl restart nginx', { stdio: 'inherit' }); }
    console.log('[SUCCESS] Nginx configured');
    return true;
  } catch (e) {
    console.log('[ERROR] Nginx setup failed:', e.message);
    return false;
  }
}

/**
 * @param {string} fqdn
 * @param {string} email
 * @returns {Promise<boolean>}
 */
async function setupSSL(fqdn, email) {
  console.log('[INFO] Obtaining SSL certificate...');
  try {
    await run('certbot', ['--nginx', '-d', fqdn, '--non-interactive', '--agree-tos', '--email', email]);
    console.log('[SUCCESS] SSL certificate configured');
    return true;
  } catch (e) {
    console.log('[ERROR] SSL setup failed:', e.message);
    return false;
  }
}

/**
 * Build the content of a Podman Quadlet .container file for the Qdrant sidecar.
 * @param {string} containerName
 * @param {string} networkName
 * @param {string} [volumePath] - host path for Qdrant storage (mounted at /qdrant/storage)
 * @returns {string}
 */
function buildQdrantQuadletContent(containerName, networkName, volumePath) {
  const lines = [
    '# Generated by container.mjs — do not edit by hand',
    '[Unit]',
    'Description=Qdrant vector database sidecar',
    'After=network-online.target',
    'Wants=network-online.target',
    '',
    '[Container]',
    `ContainerName=${containerName}`,
    `Image=${QDRANT_IMAGE}`,
    `Network=${networkName}`,
    'NetworkAlias=qdrant',
  ];
  if (volumePath) lines.push(`Volume=${volumePath}:/qdrant/storage`);
  lines.push('', '[Service]', 'Restart=always', 'RestartSec=5', '', '[Install]', 'WantedBy=multi-user.target');
  return lines.join('\n') + '\n';
}

/**
 * Build a traditional systemd [Service] unit for the Qdrant sidecar.
 * @param {string} containerName
 * @param {string} networkName
 * @param {string} [volumePath] - host path for Qdrant storage
 * @returns {string}
 */
function buildQdrantLegacyUnitContent(containerName, networkName, volumePath) {
  const volumeArg = volumePath ? `--volume ${volumePath}:/qdrant/storage ` : '';
  return [
    '# Generated by container.mjs — do not edit by hand',
    '[Unit]',
    'Description=Qdrant vector database sidecar',
    'After=network-online.target',
    'Wants=network-online.target',
    '',
    '[Service]',
    'Restart=always',
    'RestartSec=5',
    `ExecStartPre=-/usr/bin/podman rm -f ${containerName}`,
    ...(volumePath ? [`ExecStartPre=mkdir -p ${volumePath}`] : []),
    `ExecStart=/usr/bin/podman run --rm --name ${containerName} --network ${networkName} --network-alias qdrant ${volumeArg}${QDRANT_IMAGE}`,
    `ExecStop=/usr/bin/podman stop ${containerName}`,
    '',
    '[Install]',
    'WantedBy=multi-user.target',
  ].join('\n') + '\n';
}

/**
 * Build the content of a Podman Quadlet .container file.
 * Quadlet is the modern replacement for the deprecated `podman generate systemd`.
 * Systemd picks up files from /etc/containers/systemd/ after `daemon-reload`.
 * @param {{
 *   name: string,
 *   imageName: string,
 *   port: number,
 *   env?: string[],
 *   volumes?: Array<{host: string, container: string}>,
 *   extraEnv?: Array<{key: string, value: string}>,
 *   addHost?: boolean,
 *   network?: string,
 * }} cfg
 * @param {string} [kreuzbergService] - systemd service name of the kreuzberg sidecar to depend on
 * @param {string} [qdrantService] - systemd service name of the Qdrant sidecar to depend on
 * @returns {string}
 */
function buildQuadletContent(cfg, kreuzbergService, qdrantService, qdrantContainerName) {
  const { name, imageName, port, env, volumes = [], extraEnv = [], addHost, network } = cfg;
  const sidecars = [kreuzbergService, qdrantService].filter(Boolean).map(s => `${s}.service`);
  const afterTargets = sidecars.length
    ? `network-online.target ${sidecars.join(' ')}`
    : 'network-online.target';
  const lines = [
    '# Generated by container.mjs — do not edit by hand',
    '[Unit]',
    `Description=Zotero RAG — ${name}`,
    `After=${afterTargets}`,
    'Wants=network-online.target',
  ];
  for (const s of sidecars) lines.push(`Requires=${s}`);
  lines.push('', '[Container]', `ContainerName=${name}`, `Image=${imageName}`, `PublishPort=${port}:${CONTAINER_PORT}`);

  if (network) lines.push(`Network=${network}`);

  if (addHost) {
    let hostIp = 'host-gateway';
    try { hostIp = execSync("hostname -I | awk '{print $1}'", { encoding: 'utf8' }).trim(); } catch {}
    lines.push(`AddHost=host.docker.internal:${hostIp}`);
  }

  for (const { key, value } of extraEnv) lines.push(`Environment=${key}=${value}`);

  if (env) {
    for (const spec of env) {
      if (spec.includes('=')) {
        lines.push(`Environment=${spec}`);
      } else {
        const val = process.env[spec];
        if (val !== undefined) lines.push(`Environment=${spec}=${val}`);
        else console.log(`[WARNING] Env var ${spec} not found in host environment, skipping`);
      }
    }
  }

  for (const v of volumes) {
    if (!fs.existsSync(v.host)) fs.mkdirSync(v.host, { recursive: true });
    lines.push(`Volume=${v.host}:${v.container}`);
  }

  lines.push('', '[Service]', 'TimeoutStartSec=300');
  if (qdrantContainerName) {
    // $$i / $$((…)) — systemd expands $$ → $ before the shell sees the command
    lines.push(
      `ExecStartPre=/bin/sh -c 'i=0; while [ "$$i" -lt 60 ]; do /usr/bin/podman exec ${qdrantContainerName} grep -q ":18BD " /proc/net/tcp 2>/dev/null && exit 0; i=$$((i+1)); sleep 3; done; exit 1'`
    );
  }
  lines.push('Restart=always', 'RestartSec=5', '', '[Install]', 'WantedBy=multi-user.target');
  return lines.join('\n') + '\n';
}

/**
 * Build the content of a Podman Quadlet .container file for the kreuzberg sidecar.
 * @param {string} containerName
 * @param {string} networkName
 * @returns {string}
 */
function buildKreuzbergQuadletContent(containerName, networkName) {
  const kreuzbergMemory = process.env.KREUZBERG_MEMORY_LIMIT || '8g';
  return [
    '# Generated by container.mjs — do not edit by hand',
    '[Unit]',
    'Description=Kreuzberg document extraction sidecar',
    'After=network-online.target',
    'Wants=network-online.target',
    '',
    '[Container]',
    `ContainerName=${containerName}`,
    `Image=${KREUZBERG_IMAGE}`,
    `Network=${networkName}`,
    'NetworkAlias=kreuzberg',
    'AutoUpdate=registry',
    `Memory=${kreuzbergMemory}`,
    '',
    '[Service]',
    'Restart=always',
    'RestartSec=5',
    '',
    '[Install]',
    'WantedBy=multi-user.target',
  ].join('\n') + '\n';
}

/**
 * Check whether the Podman Quadlet systemd generator is present (requires Podman 4.4+).
 * @returns {boolean}
 */
function isQuadletAvailable() {
  try {
    return fs.readdirSync('/usr/lib/systemd/system-generators')
      .some(f => f.includes('podman') || f.includes('quadlet'));
  } catch {
    return false;
  }
}

/**
 * Build a traditional systemd [Service] unit for the app container.
 * Used as a fallback when Quadlet is unavailable (Podman < 4.4).
 * @param {Parameters<typeof buildQuadletContent>[0]} cfg
 * @param {string} [kreuzbergService]
 * @param {string} [qdrantService]
 * @returns {string}
 */
function buildLegacyUnitContent(cfg, kreuzbergService, qdrantService, qdrantContainerName) {
  const { name, imageName, port, env, volumes = [], extraEnv = [], addHost, network } = cfg;

  let hostIp = 'host-gateway';
  if (addHost) {
    try { hostIp = execSync("hostname -I | awk '{print $1}'", { encoding: 'utf8' }).trim(); } catch {}
  }

  const runArgs = ['--rm', '--name', name, '-p', `${port}:${CONTAINER_PORT}`];
  if (network) runArgs.push('--network', network);
  if (addHost) runArgs.push(`--add-host=host.docker.internal:${hostIp}`);
  for (const { key, value } of extraEnv) runArgs.push('-e', `${key}=${value}`);
  if (env) {
    for (const spec of env) {
      if (spec.includes('=')) {
        runArgs.push('-e', spec);
      } else {
        const val = process.env[spec];
        if (val !== undefined) runArgs.push('-e', `${spec}=${val}`);
        else console.log(`[WARNING] Env var ${spec} not found in host environment, skipping`);
      }
    }
  }
  for (const v of volumes) {
    if (!fs.existsSync(v.host)) fs.mkdirSync(v.host, { recursive: true });
    runArgs.push('-v', `${v.host}:${v.container}`);
  }
  runArgs.push(imageName);

  const sidecars = [kreuzbergService, qdrantService].filter(Boolean).map(s => `${s}.service`);
  const afterTargets = sidecars.length
    ? `network-online.target ${sidecars.join(' ')}`
    : 'network-online.target';
  const lines = [
    '# Generated by container.mjs — do not edit by hand',
    '[Unit]',
    `Description=Zotero RAG — ${name}`,
    `After=${afterTargets}`,
    'Wants=network-online.target',
  ];
  for (const s of sidecars) lines.push(`Requires=${s}`);
  lines.push('', '[Service]', 'TimeoutStartSec=300', 'Restart=always', 'RestartSec=5');
  lines.push(`ExecStartPre=-/usr/bin/podman rm -f ${name}`);
  if (qdrantContainerName) {
    // $$i / $$((…)) — systemd expands $$ → $ before the shell sees the command
    lines.push(
      `ExecStartPre=/bin/sh -c 'i=0; while [ "$$i" -lt 60 ]; do /usr/bin/podman exec ${qdrantContainerName} grep -q ":18BD " /proc/net/tcp 2>/dev/null && exit 0; i=$$((i+1)); sleep 3; done; exit 1'`
    );
  }
  lines.push(
    `ExecStart=/usr/bin/podman run ${runArgs.join(' ')}`,
    `ExecStop=/usr/bin/podman stop ${name}`,
    '', '[Install]', 'WantedBy=multi-user.target',
  );
  return lines.join('\n') + '\n';
}

/**
 * Build a traditional systemd [Service] unit for the kreuzberg sidecar.
 * Used as a fallback when Quadlet is unavailable (Podman < 4.4).
 * @param {string} containerName
 * @param {string} networkName
 * @returns {string}
 */
function buildKreuzbergLegacyUnitContent(containerName, networkName) {
  const kreuzbergMemory = process.env.KREUZBERG_MEMORY_LIMIT || '8g';
  return [
    '# Generated by container.mjs — do not edit by hand',
    '[Unit]',
    'Description=Kreuzberg document extraction sidecar',
    'After=network-online.target',
    'Wants=network-online.target',
    '',
    '[Service]',
    'Restart=always',
    'RestartSec=5',
    `ExecStartPre=-/usr/bin/podman rm -f ${containerName}`,
    `ExecStart=/usr/bin/podman run --rm --name ${containerName} --network ${networkName} --network-alias kreuzberg --memory ${kreuzbergMemory} --label io.containers.autoupdate=registry ${KREUZBERG_IMAGE}`,
    `ExecStop=/usr/bin/podman stop ${containerName}`,
    '',
    '[Install]',
    'WantedBy=multi-user.target',
  ].join('\n') + '\n';
}

/**
 * Write systemd unit files for the app and its sidecars (kreuzberg + qdrant), then enable
 * all services. Uses Podman Quadlet when available (Podman 4.4+), otherwise
 * falls back to traditional [Service] units compatible with Podman 4.3 and earlier.
 * @param {string} serviceName - main app service name
 * @param {Parameters<typeof buildQuadletContent>[0]} cfg - app container config
 * @param {{serviceName: string, containerName: string, shared: boolean}} kreuzberg
 * @param {{serviceName: string, containerName: string, shared: boolean, volumePath?: string}} qdrant
 * @returns {boolean}
 */
function setupSystemdService(serviceName, cfg, kreuzberg, qdrant) {
  const quadlet = isQuadletAvailable();
  try {
    let useLegacy = !quadlet;

    if (quadlet) {
      // Try Quadlet: write .container files and reload
      if (!fs.existsSync('/etc/containers/systemd')) fs.mkdirSync('/etc/containers/systemd', { recursive: true });
      if (!kreuzberg.shared) {
        fs.writeFileSync(
          `/etc/containers/systemd/${kreuzberg.serviceName}.container`,
          buildKreuzbergQuadletContent(kreuzberg.containerName, cfg.network || NETWORK_NAME)
        );
      }
      if (!qdrant.shared) {
        fs.writeFileSync(
          `/etc/containers/systemd/${qdrant.serviceName}.container`,
          buildQdrantQuadletContent(qdrant.containerName, cfg.network || NETWORK_NAME, qdrant.volumePath)
        );
      }
      fs.writeFileSync(
        `/etc/containers/systemd/${serviceName}.container`,
        buildQuadletContent(cfg, kreuzberg.serviceName, qdrant.serviceName, qdrant.containerName)
      );
      console.log('[INFO] Setting up systemd services via Quadlet...');
      execSync('systemctl daemon-reload', { stdio: 'inherit' });

      // Verify the Quadlet generator actually produced a unit — it can silently fail
      // (e.g. Podman 4.3 ships the generator binary but doesn't support all directives)
      try {
        execSync(`systemctl cat ${serviceName}`, { stdio: 'ignore' });
        console.log('[INFO] Quadlet units generated successfully');
      } catch {
        console.log('[WARNING] Quadlet generator did not produce a unit — falling back to legacy service units');
        try { fs.unlinkSync(`/etc/containers/systemd/${serviceName}.container`); } catch {}
        if (!kreuzberg.shared) try { fs.unlinkSync(`/etc/containers/systemd/${kreuzberg.serviceName}.container`); } catch {}
        if (!qdrant.shared) try { fs.unlinkSync(`/etc/containers/systemd/${qdrant.serviceName}.container`); } catch {}
        useLegacy = true;
      }
    }

    if (useLegacy) {
      console.log('[INFO] Setting up systemd services via legacy units...');
      if (!kreuzberg.shared) {
        const kreuzbergPath = `/etc/systemd/system/${kreuzberg.serviceName}.service`;
        fs.writeFileSync(kreuzbergPath, buildKreuzbergLegacyUnitContent(kreuzberg.containerName, cfg.network || NETWORK_NAME));
        console.log(`[INFO] Kreuzberg unit written to ${kreuzbergPath}`);
      }
      if (!qdrant.shared) {
        const qdrantPath = `/etc/systemd/system/${qdrant.serviceName}.service`;
        fs.writeFileSync(qdrantPath, buildQdrantLegacyUnitContent(qdrant.containerName, cfg.network || NETWORK_NAME, qdrant.volumePath));
        console.log(`[INFO] Qdrant unit written to ${qdrantPath}`);
      }
      const mainPath = `/etc/systemd/system/${serviceName}.service`;
      fs.writeFileSync(mainPath, buildLegacyUnitContent(cfg, kreuzberg.serviceName, qdrant.serviceName, qdrant.containerName));
      console.log(`[INFO] App unit written to ${mainPath}`);
      execSync('systemctl daemon-reload', { stdio: 'inherit' });
    }

    if (!kreuzberg.shared) {
      execSync(`systemctl enable --now ${kreuzberg.serviceName}`, { stdio: 'inherit' });
      console.log(`[SUCCESS] Kreuzberg service '${kreuzberg.serviceName}' enabled and started`);
    } else {
      console.log(`[INFO] Using shared kreuzberg service '${kreuzberg.serviceName}'`);
    }

    if (!qdrant.shared) {
      execSync(`systemctl enable --now ${qdrant.serviceName}`, { stdio: 'inherit' });
      console.log(`[SUCCESS] Qdrant service '${qdrant.serviceName}' enabled and started`);
    } else {
      console.log(`[INFO] Using shared Qdrant service '${qdrant.serviceName}'`);
    }

    execSync(`systemctl enable --now ${serviceName}`, { stdio: 'inherit' });
    console.log(`[SUCCESS] App service '${serviceName}' enabled and started`);
    console.log(`[INFO] App status:       systemctl status ${serviceName}`);
    console.log(`[INFO] App logs:         journalctl -u ${serviceName} -f`);
    if (!kreuzberg.shared) {
      console.log(`[INFO] Kreuzberg status: systemctl status ${kreuzberg.serviceName}`);
      console.log(`[INFO] Kreuzberg logs:   journalctl -u ${kreuzberg.serviceName} -f`);
    }
    if (!qdrant.shared) {
      console.log(`[INFO] Qdrant status:    systemctl status ${qdrant.serviceName}`);
      console.log(`[INFO] Qdrant logs:      journalctl -u ${qdrant.serviceName} -f`);
    }
    return true;
  } catch (e) {
    console.log(`[ERROR] Systemd service setup failed: ${e.message}`);
    return false;
  }
}

/**
 * @param {{
 *   fqdn: string, tag?: string, port?: number, name?: string,
 *   dataDir?: string, qdrantDataDir?: string, env?: string[],
 *   pull?: boolean, rebuild?: boolean, cache?: boolean, localModels?: boolean, platform?: string,
 *   nginx?: boolean, ssl?: boolean, email?: string, maxBodySize?: string,
 *   systemdService?: string, sharedKreuzberg?: string, sharedQdrant?: string, yes?: boolean,
 *   restartSidecars?: boolean, reconfigureNginx?: boolean, reconfigureSSL?: boolean
 * }} options
 */
async function handleDeploy(options) {
  console.log('Zotero RAG - Container Deploy');
  console.log('==============================');

  if (process.platform === 'win32') {
    console.log('[ERROR] Deploy is not supported on Windows (requires nginx/certbot/systemctl)');
    process.exit(1);
  }

  const useNginx = options.nginx !== false;
  const useSSL = options.ssl !== false;

  if ((useNginx || useSSL || options.systemdService) && process.getuid && process.getuid() !== 0) {
    console.log('[ERROR] Nginx/SSL/systemd setup requires sudo');
    console.log('[INFO] Run: sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy ...');
    console.log('[INFO] Or skip with --no-nginx --no-ssl (and omit --systemd-service)');
    process.exit(1);
  }

  const tag = options.tag || 'latest';
  const port = options.port || DEFAULT_PORT;
  const containerName = options.name || `${APP_NAME}-${options.fqdn.replace(/\./g, '-')}`;
  const kreuzbergName = `${containerName}-kreuzberg`;
  const email = options.email || `admin@${options.fqdn}`;
  const qdrantStoragePath = resolveQdrantStoragePath(options.qdrantDataDir, options.dataDir);

  console.log(`[INFO] FQDN: ${options.fqdn}  Container: ${containerName}  Tag: ${tag}  Port: ${port}`);
  console.log(`[INFO] nginx: ${useNginx}  ssl: ${useSSL}`);
  if (options.dataDir) console.log(`[INFO] Data dir: ${options.dataDir} -> /data`);

  if (!options.yes && !(await confirm('Continue with deployment? (y/N): '))) process.exit(0);

  // Pull or rebuild main image
  if (options.pull) {
    const remoteImage = `${REGISTRY}:${tag}`;
    console.log(`[INFO] Pulling ${remoteImage}...`);
    try {
      execSync(`${containerCmd} pull ${remoteImage}`, { stdio: 'inherit' });
      execSync(`${containerCmd} tag ${remoteImage} ${APP_NAME}:${tag}`, { stdio: 'inherit' });
    } catch (e) {
      console.log('[ERROR] Pull failed:', e.message);
      process.exit(1);
    }
  } else if (options.rebuild) {
    if (!(await buildImage(tag, options.cache === false, options.localModels === true, options.platform))) process.exit(1);
  }

  // Verify main image exists
  try {
    execSync(`${containerCmd} image inspect ${APP_NAME}:${tag}`, { stdio: 'ignore' });
  } catch {
    console.log(`[ERROR] Image ${APP_NAME}:${tag} not found. Use --pull or --rebuild.`);
    process.exit(1);
  }

  // Set up network; sidecars are started by systemd when using --systemd-service
  const qdrantName = `${containerName}-qdrant`;
  ensureNetwork(NETWORK_NAME);
  if (!options.systemdService) {
    const kreuzbergRunning = isContainerRunning(kreuzbergName);
    const qdrantRunning = isContainerRunning(qdrantName);

    if (!kreuzbergRunning || options.restartSidecars) {
      if (kreuzbergRunning) console.log('[INFO] --restart-sidecars: restarting kreuzberg sidecar...');
      await startKreuzberg(kreuzbergName, NETWORK_NAME);
    } else {
      console.log(`[INFO] Kreuzberg sidecar already running — skipping (use --restart-sidecars to force)`);
    }

    if (!qdrantRunning || options.restartSidecars) {
      if (qdrantRunning) console.log('[INFO] --restart-sidecars: restarting Qdrant sidecar...');
      await startQdrant(qdrantName, NETWORK_NAME, qdrantStoragePath);
      await waitForQdrant(qdrantName);
    } else {
      console.log(`[INFO] Qdrant sidecar already running — skipping (use --restart-sidecars to force)`);
    }
  } else {
    // systemd manages sidecars; only stop them if the caller explicitly wants a restart
    if (options.restartSidecars) {
      stopExisting(kreuzbergName);
      stopExisting(qdrantName);
    }
  }

  stopExisting(containerName);

  const volumes = options.dataDir ? [{ host: options.dataDir, container: '/data' }] : [];
  const extraEnv = [];
  if (options.dataDir) {
    extraEnv.push({ key: 'DATA_PATH', value: '/data' });
  }
  extraEnv.push({ key: 'KREUZBERG_URL', value: `http://kreuzberg:${KREUZBERG_PORT}` });
  extraEnv.push({ key: 'QDRANT_URL', value: `http://qdrant:${QDRANT_PORT}` });

  const containerCfg = {
    name: containerName,
    imageName: `${APP_NAME}:${tag}`,
    port,
    env: options.env,
    volumes,
    extraEnv,
    addHost: process.platform === 'linux',
    network: NETWORK_NAME,
  };

  if (options.systemdService) {
    const kreuzbergServiceName = options.sharedKreuzberg || `${options.systemdService}-kreuzberg`;
    const qdrantServiceName = options.sharedQdrant || `${options.systemdService}-qdrant`;
    const kreuzberg = {
      serviceName: kreuzbergServiceName,
      containerName: kreuzbergName,
      shared: !!options.sharedKreuzberg,
    };
    const qdrant = {
      serviceName: qdrantServiceName,
      containerName: qdrantName,
      shared: !!options.sharedQdrant,
      volumePath: qdrantStoragePath,
    };
    if (!setupSystemdService(options.systemdService, containerCfg, kreuzberg, qdrant)) process.exit(1);
  } else {
    try {
      await startContainer({ ...containerCfg, detach: true, restart: 'unless-stopped' });
    } catch (e) {
      console.log('[ERROR] Failed to start container:', e.message);
      process.exit(1);
    }
  }

  // Wait for container readiness
  console.log('[INFO] Waiting for container to be ready...');
  let ready = false;
  for (let i = 1; i <= 120; i++) {
    try { execSync(`curl -sf http://localhost:${port}/health`, { stdio: 'ignore' }); ready = true; break; }
    catch { if (i % 10 === 0) console.log(`[INFO] Attempt ${i}/120...`); await new Promise(r => setTimeout(r, 2000)); }
  }
  if (!ready) console.log('[WARNING] Container may not be fully ready, continuing anyway...');

  if (useNginx) {
    if (!isNginxConfigured(options.fqdn) || options.reconfigureNginx) {
      if (isNginxConfigured(options.fqdn)) console.log('[INFO] --reconfigure-nginx: updating existing nginx config...');
      setupNginx(options.fqdn, port, options.maxBodySize);
    } else {
      console.log('[INFO] nginx already configured — skipping (use --reconfigure-nginx to update)');
    }
  }
  if (useSSL) {
    if (!isSSLConfigured(options.fqdn) || options.reconfigureSSL) {
      if (isSSLConfigured(options.fqdn)) console.log('[INFO] --reconfigure-ssl: re-running certbot...');
      await setupSSL(options.fqdn, email);
    } else {
      console.log('[INFO] SSL certificate already exists — skipping certbot (use --reconfigure-ssl to renew/reconfigure)');
    }
  }

  console.log('\n[SUCCESS] Deployment complete!');
  const scheme = useSSL ? 'https' : 'http';
  console.log(`[INFO] URL: ${scheme}://${options.fqdn}`);
  if (!options.systemdService) {
    console.log(`[INFO] App logs:       ${containerCmd} logs -f ${containerName}`);
    console.log(`[INFO] Kreuzberg logs: ${containerCmd} logs -f ${kreuzbergName}`);
    console.log(`[INFO] Qdrant logs:    ${containerCmd} logs -f ${qdrantName}`);
  }
}

// ============================================================================
// Migrate — shared helpers
// ============================================================================

/**
 * Find the running app container and return its image, network, and env vars.
 * Exits the process with a helpful message if no container is found.
 * @param {string} [preferredName] - Container name override from --name option.
 * @returns {{ appContainerName: string, appImage: string, networkName: string, appEnv: string[] }}
 */
function getRunningAppContainer(preferredName) {
  // Parse running containers from JSON (handles both Docker and Podman output)
  const psRaw = execSync(`${containerCmd} ps --format json`, { encoding: 'utf8' }).trim() || '[]';
  let psData;
  try {
    psData = JSON.parse(psRaw);
    if (!Array.isArray(psData)) psData = [psData];
  } catch {
    psData = psRaw.split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  }
  const allNames = psData.flatMap(c => {
    const n = c.Names ?? c.Name ?? c.names ?? c.name ?? [];
    return Array.isArray(n) ? n : [n];
  }).filter(Boolean);

  const appContainerName = preferredName || allNames.find(n => n.includes(APP_NAME) && !n.endsWith('-qdrant') && !n.endsWith('-kreuzberg'));
  if (!appContainerName) {
    console.error('[ERROR] No running app container found. Use --name to specify it.');
    if (allNames.length) {
      console.error(`[INFO]  Running containers: ${allNames.join(', ')}`);
    } else {
      console.error('[INFO]  No containers appear to be running.');
    }
    console.error('[HINT]  On Linux you may need to prefix the command with:');
    console.error('          sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs ...');
    process.exit(1);
  }

  let inspect;
  try {
    inspect = JSON.parse(execSync(`${containerCmd} inspect ${appContainerName}`, { encoding: 'utf8' }));
  } catch (e) {
    console.error(`[ERROR] Could not inspect '${appContainerName}': ${e.message}`);
    process.exit(1);
  }

  const info = inspect[0];
  const appImage = info.Config.Image;
  const networkName = Object.keys(info.NetworkSettings.Networks)[0] || NETWORK_NAME;
  const appEnv = /** @type {string[]} */ (info.Config?.Env ?? []);

  console.log(`[INFO] Using app container: ${appContainerName}`);
  return { appContainerName, appImage, networkName, appEnv };
}

/**
 * Run a script inside a temporary container on the app's internal network.
 * The project directory is mounted at /project; the container's own venv at
 * /app/.venv is used so the project mount does not shadow it.
 * Environment variables from the running app container are forwarded so that
 * settings.py uses the correct paths rather than values from the local .env file.
 *
 * @param {{ appImage: string, networkName: string, appEnv: string[] }} containerInfo
 * @param {string[]} scriptCmd - Command + args to run inside the container (e.g. ['/app/.venv/bin/python', '/project/scripts/foo.py'])
 * @param {string[]} [extraEnv] - Additional KEY=VALUE strings to pass via -e
 */
function runInTempContainer(containerInfo, scriptCmd, extraEnv = []) {
  const { appImage, networkName, appEnv } = containerInfo;
  const projectDir = process.cwd();

  // Forward env vars that affect settings.py paths so the local .env is not used.
  const forwardKeys = ['DATA_PATH', 'QDRANT_URL', 'MODEL_WEIGHTS_PATH', 'VECTOR_DB_PATH'];
  const forwarded = appEnv
    .filter(e => forwardKeys.some(k => e.startsWith(`${k}=`)))
    .flatMap(e => ['-e', e]);

  const extraEnvFlags = extraEnv.flatMap(e => ['-e', e]);

  const runArgs = [
    'run', '--rm',
    '--network', networkName,
    '-v', `${projectDir}:/project`,
    '-w', '/project',
    ...forwarded,
    ...extraEnvFlags,
    appImage,
    ...scriptCmd,
  ];

  console.log(`[INFO] Image: ${appImage}  Network: ${networkName}  Project: ${projectDir}`);
  console.log('[INFO] Running in a temporary container on the internal network...\n');
  execSync(`${containerCmd} ${runArgs.join(' ')}`, { stdio: 'inherit' });
}

/**
 * @param {{name?: string, oldId?: string, newId?: string, dryRun?: boolean, list?: boolean}} options
 */
async function handleMigrateLibraryId(options) {
  console.log('Zotero RAG - Library ID Migration');
  console.log('===================================');

  const containerInfo = getRunningAppContainer(options.name);

  const scriptArgs = ['/project/scripts/migrate_library_id.py', '--qdrant-url', 'http://qdrant:6333'];
  if (options.oldId) scriptArgs.push('--old-id', options.oldId);
  if (options.newId) scriptArgs.push('--new-id', options.newId);
  if (options.dryRun) scriptArgs.push('--dry-run');
  if (options.list) scriptArgs.push('--list');

  try {
    runInTempContainer(containerInfo, ['/app/.venv/bin/python', ...scriptArgs]);
    console.log('\n[SUCCESS] Migration complete.');
  } catch (e) {
    console.error('\n[ERROR] Migration script failed:', e.message);
    process.exit(1);
  }
}

// ============================================================================
// CLI Setup
// ============================================================================

detectContainerTool();

const program = new Command();
program.name('container').description('Container management for Zotero RAG').version('1.0.0');

/** @param {string} v @param {string[]} prev @returns {string[]} */
const collect = (v, prev) => (prev ? [...prev, v] : [v]);

program
  .command('build')
  .description('Build container image locally')
  .option('--tag <tag>', 'Version tag (default: auto from git)')
  .option('--no-cache', 'Force rebuild all layers')
  .option('--local-models', 'Install sentence-transformers/torch for local presets (~1-2 GB extra; off by default)')
  .option('--platform <platform>', 'Target platform, e.g. linux/amd64 or linux/arm64 (default: host arch)')
  .option('--yes', 'Skip confirmation')
  .action(handleBuild);

program
  .command('push')
  .description('Build and push image to Docker Hub')
  .option('--tag <tag>', 'Version tag (default: auto from git)')
  .option('--no-build', 'Skip build, push existing image only')
  .option('--no-cache', 'Force rebuild all layers')
  .option('--local-models', 'Install sentence-transformers/torch (~1-2 GB extra; off by default)')
  .option('--platform <platform>', 'Target platform, e.g. linux/amd64 (default: host arch)')
  .option('--yes', 'Skip confirmation')
  .action(handlePush);

program
  .command('start')
  .description('Start the app container and sidecars (kreuzberg + qdrant)')
  .option('--tag <tag>', 'Image tag (default: latest)')
  .option('--name <name>', `Container name (default: ${APP_NAME}-<tag>)`)
  .option('--port <port>', `Host port (default: ${DEFAULT_PORT})`, parseInt)
  .option('--data-dir <dir>', 'Host path mounted at /data')
  .option('--zotero-host <url>', `Zotero API URL (default: ${DEFAULT_ZOTERO_HOST})`)
  .option('--env <var>', 'Env var KEY or KEY=VAL (repeatable)', collect, [])
  .option('--volume <mapping>', 'Volume HOST:CONTAINER (repeatable)', collect, [])
  .option('--restart <policy>', 'Restart policy (no|on-failure|always|unless-stopped)')
  .option('--no-detach', 'Run in foreground')
  .option('--qdrant-data-dir <dir>', 'Host path for Qdrant persistent storage (default: <data-dir>/qdrant-server)')
  .option('--no-kreuzberg', 'Skip kreuzberg sidecar (use if running kreuzberg separately)')
  .option('--no-qdrant', 'Skip Qdrant sidecar (use if running Qdrant separately)')
  .action(handleStart);

program
  .command('stop')
  .description('Stop the app container and its sidecars (kreuzberg + qdrant)')
  .option('--name <name>', `Container name (default: ${APP_NAME}-latest)`)
  .option('--all', `Stop all ${APP_NAME} containers`)
  .option('--remove', 'Remove containers and network after stopping')
  .action(handleStop);

program
  .command('restart')
  .description('Restart the app container and kreuzberg sidecar')
  .option('--name <name>', `Container name (default: ${APP_NAME}-latest)`)
  .option('--tag <tag>', 'Image tag (if creating new container)')
  .option('--port <port>', 'Host port (if creating new container)', parseInt)
  .option('--data-dir <dir>', 'Data directory (if creating new container)')
  .option('--zotero-host <url>', 'Zotero API URL (if creating new container)')
  .option('--env <var>', 'Env var (if creating new container, repeatable)', collect, [])
  .option('--volume <mapping>', 'Volume (if creating new container, repeatable)', collect, [])
  .option('--restart <policy>', 'Restart policy')
  .action(handleRestart);

program
  .command('logs')
  .description('View container logs')
  .option('--name <name>', `Container name (default: ${APP_NAME}-latest)`)
  .option('-f, --follow', 'Follow log output')
  .option('--tail <lines>', 'Number of lines from end', parseInt)
  .option('--kreuzberg', 'Show kreuzberg sidecar logs instead of app logs')
  .option('--qdrant', 'Show Qdrant sidecar logs instead of app logs')
  .action(handleLogs);

program
  .command('deploy')
  .description('Deploy container with nginx reverse proxy and SSL (requires sudo for nginx/SSL)')
  .requiredOption('--fqdn <fqdn>', 'Fully qualified domain name')
  .option('--tag <tag>', 'Image tag (default: latest)')
  .option('--port <port>', `Host port (default: ${DEFAULT_PORT})`, parseInt)
  .option('--name <name>', 'Container name')
  .option('--data-dir <dir>', 'Persistent data directory (mounted at /data)')
  .option('--qdrant-data-dir <dir>', 'Host path for Qdrant persistent storage (default: <data-dir>/qdrant-server)')
  .option('--env <var>', 'Env var KEY or KEY=VAL (repeatable)', collect, [])
  .option('--pull', 'Pull image from registry before deploying')
  .option('--rebuild', 'Rebuild image locally before deploying')
  .option('--no-cache', 'Disable layer cache (with --rebuild)')
  .option('--local-models', 'Install sentence-transformers/torch when rebuilding (~1-2 GB extra; off by default)')
  .option('--platform <platform>', 'Target platform when rebuilding, e.g. linux/amd64')
  .option('--no-nginx', 'Skip nginx configuration')
  .option('--no-ssl', 'Skip SSL certificate setup')
  .option('--max-body-size <size>', 'nginx client_max_body_size for uploads (default: 300M)', '300M')
  .option('--email <email>', 'Email for certbot (default: admin@<fqdn>)')
  .option('--systemd-service <name>', 'Create/replace a Quadlet systemd service with this name (requires sudo; env: DEPLOY_SYSTEMD_SERVICE)')
  .option('--shared-kreuzberg <name>', 'Use an existing kreuzberg systemd service instead of creating one (env: DEPLOY_SHARED_KREUZBERG)')
  .option('--shared-qdrant <name>', 'Use an existing Qdrant systemd service instead of creating one')
  .option('--yes', 'Skip confirmation')
  .option('--restart-sidecars', 'Restart kreuzberg/qdrant sidecars even if already running (default: skip if already running)')
  .option('--reconfigure-nginx', 'Rewrite nginx config even if already present')
  .option('--reconfigure-ssl', 'Run certbot even if SSL certificate already exists')
  .addHelpText('after', `
Examples:
  # Deploy with nginx + SSL
  sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy \\
    --fqdn rag.example.com --data-dir /srv/zotero-rag/data --pull

  # Deploy without nginx/SSL (container only)
  node bin/container.mjs deploy --fqdn localhost --no-nginx --no-ssl --pull

  # Deploy with systemd Quadlet services (auto-restarts, survives reboots)
  sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy \\
    --fqdn rag.example.com --data-dir /srv/zotero-rag/data --pull \\
    --systemd-service zotero-rag

  # Deploy a second instance sharing existing sidecar services
  sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy \\
    --fqdn rag2.example.com --data-dir /srv/zotero-rag2/data --pull \\
    --systemd-service zotero-rag2 --shared-kreuzberg zotero-rag-kreuzberg \\
    --shared-qdrant zotero-rag-qdrant

  # Deploy with extra env vars
  sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy \\
    --fqdn rag.example.com --env OPENAI_API_KEY=sk-... --pull
`)
  .action(handleDeploy);

const migrate = program
  .command('migrate')
  .description('Run data migrations against the live Qdrant instance');

migrate
  .command('library-id')
  .description('Rename a library_id across all Qdrant collections')
  .option('--name <name>', 'Full Qdrant container name (auto-detected if omitted)')
  .option('--old-id <id>', 'Current library_id to rename (e.g. 1)')
  .option('--new-id <id>', 'New library_id (e.g. u3866263)')
  .option('--list', 'List all distinct library_ids (no migration)')
  .option('--dry-run', 'Preview changes without writing anything')
  .action(handleMigrateLibraryId);

program.parse();
