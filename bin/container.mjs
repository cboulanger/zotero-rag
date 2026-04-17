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

  const args = [
    'run', '-d',
    '--name', kreuzbergName,
    '--network', networkName,
    '--network-alias', 'kreuzberg',
    '--restart', 'unless-stopped',
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
      execSync(`${containerCmd} rm ${name}`, { stdio: 'inherit' });
    }
  } catch {}
}

/**
 * @param {{
 *   tag?: string, name?: string, port?: number, detach?: boolean,
 *   dataDir?: string, zoteroHost?: string, env?: string[],
 *   volume?: string[], restart?: string, noKreuzberg?: boolean
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

  const imageName = resolveImage(tag);
  stopExisting(name);

  // Set up shared network and kreuzberg sidecar
  if (!options.noKreuzberg) {
    ensureNetwork(NETWORK_NAME);
    await startKreuzberg(kreuzbergName, NETWORK_NAME);
  }

  const volumes = [];
  const extraEnv = [];

  if (options.dataDir) {
    volumes.push({ host: options.dataDir, container: '/data' });
    extraEnv.push({ key: 'VECTOR_DB_PATH', value: '/data/qdrant' });
    extraEnv.push({ key: 'MODEL_WEIGHTS_PATH', value: '/data/models' });
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

  const addHost = process.platform === 'linux';

  console.log(`[INFO] name=${name} image=${imageName} port=${port} zotero=${zoteroHost}`);

  try {
    await startContainer({
      name, imageName, port, detach,
      restart: options.restart,
      env: options.env,
      volumes, extraEnv, addHost,
      network: options.noKreuzberg ? undefined : NETWORK_NAME,
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

  // Stop the associated kreuzberg sidecar
  stopKreuzberg(`${name}-kreuzberg`, options.remove !== false);
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
      // Restart kreuzberg sidecar too
      const kreuzbergName = `${name}-kreuzberg`;
      const kreuzbergId = execSync(
        `${containerCmd} ps -a --filter "name=^${kreuzbergName}$" --format "{{.ID}}"`,
        { encoding: 'utf8', stdio: 'pipe' }
      ).trim();
      if (kreuzbergId) execSync(`${containerCmd} stop ${kreuzbergName}`, { stdio: 'inherit' });

      execSync(`${containerCmd} start ${name}`, { stdio: 'inherit' });
      if (kreuzbergId) execSync(`${containerCmd} start ${kreuzbergName}`, { stdio: 'inherit' });
      console.log(`[SUCCESS] Restarted ${name} + ${kreuzbergName}`);
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
 * @param {{name?: string, follow?: boolean, tail?: number, kreuzberg?: boolean}} options
 */
async function handleLogs(options) {
  const name = options.kreuzberg
    ? `${options.name || `${APP_NAME}-latest`}-kreuzberg`
    : (options.name || `${APP_NAME}-latest`);
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
 * @param {string} fqdn
 * @param {number} port
 * @returns {boolean}
 */
function setupNginx(fqdn, port) {
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
        client_max_body_size 100M;
        proxy_read_timeout 300s;
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
 * @param {{
 *   fqdn: string, tag?: string, port?: number, name?: string,
 *   dataDir?: string, env?: string[],
 *   pull?: boolean, rebuild?: boolean, cache?: boolean, localModels?: boolean, platform?: string,
 *   nginx?: boolean, ssl?: boolean, email?: string, yes?: boolean
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

  if ((useNginx || useSSL) && process.getuid && process.getuid() !== 0) {
    console.log('[ERROR] Nginx/SSL setup requires sudo');
    console.log('[INFO] Run: sudo env "PATH=$PATH" node bin/container.mjs deploy ...');
    console.log('[INFO] Or skip with --no-nginx --no-ssl');
    process.exit(1);
  }

  const tag = options.tag || 'latest';
  const port = options.port || DEFAULT_PORT;
  const containerName = options.name || `${APP_NAME}-${options.fqdn.replace(/\./g, '-')}`;
  const kreuzbergName = `${containerName}-kreuzberg`;
  const email = options.email || `admin@${options.fqdn}`;

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

  // Set up network and kreuzberg sidecar
  ensureNetwork(NETWORK_NAME);
  await startKreuzberg(kreuzbergName, NETWORK_NAME);

  stopExisting(containerName);

  const volumes = options.dataDir ? [{ host: options.dataDir, container: '/data' }] : [];
  const extraEnv = [];
  if (options.dataDir) {
    extraEnv.push({ key: 'VECTOR_DB_PATH', value: '/data/qdrant' });
    extraEnv.push({ key: 'MODEL_WEIGHTS_PATH', value: '/data/models' });
  }
  extraEnv.push({ key: 'KREUZBERG_URL', value: `http://kreuzberg:${KREUZBERG_PORT}` });

  try {
    await startContainer({
      name: containerName,
      imageName: `${APP_NAME}:${tag}`,
      port,
      detach: true,
      restart: 'unless-stopped',
      env: options.env,
      volumes,
      extraEnv,
      addHost: true,
      network: NETWORK_NAME,
    });
  } catch (e) {
    console.log('[ERROR] Failed to start container:', e.message);
    process.exit(1);
  }

  // Wait for container readiness
  console.log('[INFO] Waiting for container to be ready...');
  let ready = false;
  for (let i = 1; i <= 30; i++) {
    try { execSync(`curl -sf http://localhost:${port}/health`, { stdio: 'ignore' }); ready = true; break; }
    catch { if (i % 5 === 0) console.log(`[INFO] Attempt ${i}/30...`); await new Promise(r => setTimeout(r, 2000)); }
  }
  if (!ready) console.log('[WARNING] Container may not be fully ready, continuing anyway...');

  if (useNginx) setupNginx(options.fqdn, port);
  if (useSSL) await setupSSL(options.fqdn, email);

  console.log('\n[SUCCESS] Deployment complete!');
  const scheme = useSSL ? 'https' : 'http';
  console.log(`[INFO] URL: ${scheme}://${options.fqdn}`);
  console.log(`[INFO] App logs:       ${containerCmd} logs -f ${containerName}`);
  console.log(`[INFO] Sidecar logs:   ${containerCmd} logs -f ${kreuzbergName}`);
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
  .description('Start the app container and kreuzberg sidecar')
  .option('--tag <tag>', 'Image tag (default: latest)')
  .option('--name <name>', `Container name (default: ${APP_NAME}-<tag>)`)
  .option('--port <port>', `Host port (default: ${DEFAULT_PORT})`, parseInt)
  .option('--data-dir <dir>', 'Host path mounted at /data')
  .option('--zotero-host <url>', `Zotero API URL (default: ${DEFAULT_ZOTERO_HOST})`)
  .option('--env <var>', 'Env var KEY or KEY=VAL (repeatable)', collect, [])
  .option('--volume <mapping>', 'Volume HOST:CONTAINER (repeatable)', collect, [])
  .option('--restart <policy>', 'Restart policy (no|on-failure|always|unless-stopped)')
  .option('--no-detach', 'Run in foreground')
  .option('--no-kreuzberg', 'Skip kreuzberg sidecar (use if running kreuzberg separately)')
  .action(handleStart);

program
  .command('stop')
  .description('Stop the app container and its kreuzberg sidecar')
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
  .action(handleLogs);

program
  .command('deploy')
  .description('Deploy container with nginx reverse proxy and SSL (requires sudo for nginx/SSL)')
  .requiredOption('--fqdn <fqdn>', 'Fully qualified domain name')
  .option('--tag <tag>', 'Image tag (default: latest)')
  .option('--port <port>', `Host port (default: ${DEFAULT_PORT})`, parseInt)
  .option('--name <name>', 'Container name')
  .option('--data-dir <dir>', 'Persistent data directory (mounted at /data)')
  .option('--env <var>', 'Env var KEY or KEY=VAL (repeatable)', collect, [])
  .option('--pull', 'Pull image from registry before deploying')
  .option('--rebuild', 'Rebuild image locally before deploying')
  .option('--no-cache', 'Disable layer cache (with --rebuild)')
  .option('--local-models', 'Install sentence-transformers/torch when rebuilding (~1-2 GB extra; off by default)')
  .option('--platform <platform>', 'Target platform when rebuilding, e.g. linux/amd64')
  .option('--no-nginx', 'Skip nginx configuration')
  .option('--no-ssl', 'Skip SSL certificate setup')
  .option('--email <email>', 'Email for certbot (default: admin@<fqdn>)')
  .option('--yes', 'Skip confirmation')
  .addHelpText('after', `
Examples:
  # Deploy with nginx + SSL
  sudo env "PATH=$PATH" node bin/container.mjs deploy \\
    --fqdn rag.example.com --data-dir /srv/zotero-rag/data --pull

  # Deploy without nginx/SSL (container only)
  node bin/container.mjs deploy --fqdn localhost --no-nginx --no-ssl --pull

  # Deploy with extra env vars
  sudo env "PATH=$PATH" node bin/container.mjs deploy \\
    --fqdn rag.example.com --env OPENAI_API_KEY=sk-... --pull
`)
  .action(handleDeploy);

program.parse();
