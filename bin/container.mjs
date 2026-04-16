#!/usr/bin/env node

/**
 * Container Management Script for Zotero RAG
 *
 * Handles building, pushing, starting, stopping, restarting, logs, and deploying
 * the Zotero RAG backend container. Automatically detects Docker or Podman.
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
const DEFAULT_PORT = 8119;
const CONTAINER_PORT = 8119;
const DEFAULT_ZOTERO_HOST = 'http://host.docker.internal:23119';

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
    return false; // binary not found
  }
  try {
    execSync(`${cmd} info`, { stdio: 'ignore' });
    return true;
  } catch {
    return false; // binary exists but daemon not running / socket not connected
  }
}

/**
 * Detect container tool (docker or podman, prefer docker).
 * Verifies that the daemon is actually reachable, not just that the binary exists.
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
// Build
// ============================================================================

/**
 * @param {string} tag
 * @param {boolean} noCache
 * @param {boolean} installOcr
 * @param {boolean} installLocalModels
 * @param {boolean|undefined} installBuildTools  undefined = auto (arm64 only)
 * @returns {Promise<boolean>}
 */
async function buildImage(tag, noCache, installOcr = true, installLocalModels = false, installBuildTools = undefined) {
  const fullTag = `${APP_NAME}:${tag}`;
  const buildToolsLabel = installBuildTools === true ? 'forced' : installBuildTools === false ? 'skipped' : 'auto (arm64)';
  console.log(`[INFO] Building ${fullTag} (OCR: ${installOcr ? 'included' : 'skipped'}, local-models: ${installLocalModels ? 'included' : 'skipped'}, build-tools: ${buildToolsLabel})...`);
  if (installLocalModels) {
    console.log('[INFO] --local-models: sentence-transformers/torch will be installed (~1-2 GB extra)');
  }
  const args = ['build'];
  if (noCache) args.push('--no-cache');
  args.push('--build-arg', `INSTALL_OCR=${installOcr}`);
  args.push('--build-arg', `INSTALL_LOCAL_MODELS=${installLocalModels}`);
  if (installBuildTools !== undefined) {
    args.push('--build-arg', `INSTALL_BUILD_TOOLS=${installBuildTools}`);
  }
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
 * @param {{tag?: string, cache?: boolean, ocr?: boolean, localModels?: boolean, buildTools?: boolean, yes?: boolean}} options
 */
async function handleBuild(options) {
  console.log('Zotero RAG - Container Build');
  console.log('=============================');
  const tag = resolveTag(options.tag);
  const installOcr = options.ocr !== false;
  const installLocalModels = options.localModels === true;
  const installBuildTools = options.buildTools;  // undefined = auto
  console.log(`[INFO] Tag: ${tag}  OCR: ${installOcr}  local-models: ${installLocalModels}`);
  if (!options.yes && !(await confirm('Continue with build? (y/N): '))) process.exit(0);
  if (!(await buildImage(tag, options.cache === false, installOcr, installLocalModels, installBuildTools))) process.exit(1);
  console.log(`[INFO] To push: node bin/container.mjs push --tag ${tag}`);
}

// ============================================================================
// Push
// ============================================================================

/**
 * Tag local image for registry
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
 * Login to Docker Hub via stdin pipe
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
 * Push image (and latest tag) to registry
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
 * @param {{tag?: string, build?: boolean, cache?: boolean, ocr?: boolean, localModels?: boolean, buildTools?: boolean, yes?: boolean}} options
 */
async function handlePush(options) {
  console.log('Zotero RAG - Container Push');
  console.log('============================');
  validateRegistryEnv();
  const tag = resolveTag(options.tag);
  const doBuild = options.build !== false;
  const installOcr = options.ocr !== false;
  const installLocalModels = options.localModels === true;
  const installBuildTools = options.buildTools;
  console.log(`[INFO] Tag: ${tag}  Registry: ${credentials.username}/${APP_NAME}  Build: ${doBuild}  OCR: ${installOcr}  local-models: ${installLocalModels}`);
  if (!options.yes && !(await confirm(`Continue with ${doBuild ? 'build + ' : ''}push? (y/N): `))) process.exit(0);

  process.on('exit', () => { try { execSync(`${containerCmd} logout docker.io`, { stdio: 'ignore' }); } catch {} });

  if (doBuild) {
    if (!(await buildImage(tag, options.cache === false, installOcr, installLocalModels, installBuildTools))) process.exit(1);
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
 *   addHost?: boolean
 * }} cfg
 */
async function startContainer(cfg) {
  const { name, imageName, port, detach = true, restart, env, volumes = [], extraEnv = [], addHost } = cfg;

  const args = ['run', detach ? '-d' : '', '--name', name, '-p', `${port}:${CONTAINER_PORT}`].filter(Boolean);

  if (restart) args.push('--restart', restart);
  if (addHost) args.push('--add-host=host.docker.internal:host-gateway');

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
 * @returns {string} imageName to pass to docker run
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
 *   volume?: string[], restart?: string
 * }} options
 */
async function handleStart(options) {
  console.log('Zotero RAG - Container Start');
  console.log('=============================');
  const tag = options.tag || 'latest';
  const name = options.name || `${APP_NAME}-${tag}`;
  const port = options.port || DEFAULT_PORT;
  const detach = options.detach !== false;

  const imageName = resolveImage(tag);
  stopExisting(name);

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
  extraEnv.push({ key: 'ZOTERO_API_URL', value: zoteroHost });

  const addHost = process.platform === 'linux';

  console.log(`[INFO] name=${name} image=${imageName} port=${port} zotero=${zoteroHost}`);

  try {
    await startContainer({ name, imageName, port, detach, restart: options.restart, env: options.env, volumes, extraEnv, addHost });
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
    return;
  }

  const name = options.name || `${APP_NAME}-latest`;
  const id = execSync(`${containerCmd} ps -a --filter "name=^${name}$" --format "{{.ID}}"`, { encoding: 'utf8', stdio: 'pipe' }).trim();
  if (!id) { console.error(`[ERROR] Container '${name}' not found`); process.exit(1); }
  execSync(`${containerCmd} stop ${name}`, { stdio: 'inherit' });
  if (options.remove) execSync(`${containerCmd} rm ${name}`, { stdio: 'inherit' });
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
      execSync(`${containerCmd} start ${name}`, { stdio: 'inherit' });
      console.log(`[SUCCESS] Restarted ${name}`);
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
 * @param {{name?: string, follow?: boolean, tail?: number}} options
 */
async function handleLogs(options) {
  const name = options.name || `${APP_NAME}-latest`;
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
 * Write nginx config and enable the site
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

    client_max_body_size 100M;
    proxy_read_timeout 300s;
    proxy_connect_timeout 300s;
    proxy_send_timeout 300s;

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
 * Obtain SSL certificate via certbot
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
 *   pull?: boolean, rebuild?: boolean, cache?: boolean, ocr?: boolean,
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
  const email = options.email || `admin@${options.fqdn}`;

  console.log(`[INFO] FQDN: ${options.fqdn}  Container: ${containerName}  Tag: ${tag}  Port: ${port}`);
  console.log(`[INFO] nginx: ${useNginx}  ssl: ${useSSL}`);
  if (options.dataDir) console.log(`[INFO] Data dir: ${options.dataDir} -> /data`);

  if (!options.yes && !(await confirm('Continue with deployment? (y/N): '))) process.exit(0);

  // Pull or rebuild image
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
    if (!(await buildImage(tag, options.cache === false, options.ocr !== false, options.localModels === true, options.buildTools))) process.exit(1);
  }

  // Verify image exists
  try {
    execSync(`${containerCmd} image inspect ${APP_NAME}:${tag}`, { stdio: 'ignore' });
  } catch {
    console.log(`[ERROR] Image ${APP_NAME}:${tag} not found. Use --pull or --rebuild.`);
    process.exit(1);
  }

  stopExisting(containerName);

  const volumes = options.dataDir ? [{ host: options.dataDir, container: '/data' }] : [];
  const extraEnv = [];
  if (options.dataDir) {
    extraEnv.push({ key: 'VECTOR_DB_PATH', value: '/data/qdrant' });
    extraEnv.push({ key: 'MODEL_WEIGHTS_PATH', value: '/data/models' });
  }
  // On Linux the container needs to reach host.docker.internal
  extraEnv.push({ key: 'ZOTERO_API_URL', value: DEFAULT_ZOTERO_HOST });

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
      addHost: true  // deploy always on Linux
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
  console.log(`[INFO] Logs: ${containerCmd} logs -f ${containerName}`);
}

// ============================================================================
// CLI Setup
// ============================================================================

detectContainerTool();

const program = new Command();
program.name('container').description('Container management for Zotero RAG').version('1.0.0');

/** @param {(v: string, prev: string[]) => string[]} */
const collect = (v, prev) => (prev ? [...prev, v] : [v]);

program
  .command('build')
  .description('Build container image locally')
  .option('--tag <tag>', 'Version tag (default: auto from git)')
  .option('--no-cache', 'Force rebuild all layers')
  .option('--no-ocr', 'Exclude Tesseract OCR (smaller image; set OCR_ENABLED=false at runtime)')
  .option('--local-models', 'Install sentence-transformers/torch for local presets (~1-2 GB extra; off by default)')
  .option('--build-tools', 'Force-install gcc build tools (default: auto-detected on arm64)')
  .option('--no-build-tools', 'Skip gcc install even on arm64')
  .option('--yes', 'Skip confirmation')
  .action(handleBuild);

program
  .command('push')
  .description('Build and push image to Docker Hub')
  .option('--tag <tag>', 'Version tag (default: auto from git)')
  .option('--no-build', 'Skip build, push existing image only')
  .option('--no-cache', 'Force rebuild all layers')
  .option('--no-ocr', 'Exclude Tesseract OCR (smaller image)')
  .option('--local-models', 'Install sentence-transformers/torch (~1-2 GB extra; off by default)')
  .option('--build-tools', 'Force-install gcc build tools (default: auto-detected on arm64)')
  .option('--no-build-tools', 'Skip gcc install even on arm64')
  .option('--yes', 'Skip confirmation')
  .action(handlePush);

program
  .command('start')
  .description('Start a container')
  .option('--tag <tag>', 'Image tag (default: latest)')
  .option('--name <name>', `Container name (default: ${APP_NAME}-<tag>)`)
  .option('--port <port>', `Host port (default: ${DEFAULT_PORT})`, parseInt)
  .option('--data-dir <dir>', 'Host path mounted at /data')
  .option('--zotero-host <url>', `Zotero API URL (default: ${DEFAULT_ZOTERO_HOST})`)
  .option('--env <var>', 'Env var KEY or KEY=VAL (repeatable)', collect, [])
  .option('--volume <mapping>', 'Volume HOST:CONTAINER (repeatable)', collect, [])
  .option('--restart <policy>', 'Restart policy (no|on-failure|always|unless-stopped)')
  .option('--no-detach', 'Run in foreground')
  .action(handleStart);

program
  .command('stop')
  .description('Stop a running container')
  .option('--name <name>', `Container name (default: ${APP_NAME}-latest)`)
  .option('--all', `Stop all ${APP_NAME} containers`)
  .option('--remove', 'Remove container after stopping')
  .action(handleStop);

program
  .command('restart')
  .description('Restart a container')
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
  .option('--no-ocr', 'Exclude Tesseract when rebuilding (smaller image; also set OCR_ENABLED=false)')
  .option('--local-models', 'Install sentence-transformers/torch when rebuilding (~1-2 GB extra; off by default)')
  .option('--build-tools', 'Force-install gcc when rebuilding (default: auto-detected on arm64)')
  .option('--no-build-tools', 'Skip gcc install even on arm64 when rebuilding')
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
