#!/usr/bin/env node

/**
 * Deployment Wrapper Script
 *
 * Reads environment variables from a file and deploys the Zotero RAG container
 * using bin/container.mjs deploy with appropriate parameters.
 *
 * Usage:
 *   node bin/deploy.mjs <env-file>
 *
 * Examples:
 *   node bin/deploy.mjs .env.deploy.myserver
 *   node bin/deploy.mjs /path/to/production.env
 *
 * Mapping:
 *   DEPLOY_*    → --option flags for the deploy command
 *   Everything else → --env KEY  (value loaded into environment via dotenv.config)
 *
 * Boolean DEPLOY_ values:
 *   1 / true / on  → flag is included  (e.g. DEPLOY_PULL=true → --pull)
 *   0 / false / off → flag is omitted
 *   Any other value → treated as a string option value
 */

import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { parse as dotenvParse, config as dotenvConfig } from 'dotenv';

/**
 * Parse the env file and split entries into deploy options and container env vars
 * @param {string} envFilePath
 * @returns {{ deployOptions: string[], containerEnv: string[] }}
 */
function parseEnvFile(envFilePath) {
  if (!fs.existsSync(envFilePath)) {
    console.error(`[ERROR] File not found: ${envFilePath}`);
    process.exit(1);
  }

  const parsed = dotenvParse(fs.readFileSync(envFilePath, 'utf8'));
  const deployOptions = [];
  const containerEnv = [];

  for (const [key, value] of Object.entries(parsed)) {
    if (key.startsWith('DEPLOY_')) {
      // Convert DEPLOY_FOO_BAR → --foo-bar
      const flag = key.slice(7).toLowerCase().replace(/_/g, '-');
      const truthy = ['1', 'true', 'on'].includes(value.toLowerCase());
      const falsy = ['', '0', 'false', 'off'].includes(value.toLowerCase());

      if (truthy) {
        deployOptions.push(`--${flag}`);
      } else if (!falsy) {
        deployOptions.push(`--${flag}`, value);
      }
      // falsy → omit the flag entirely
    } else {
      // Regular env var — pass name only; value comes from process.env after dotenv.config
      containerEnv.push('--env', key);
    }
  }

  return { deployOptions, containerEnv };
}

function main() {
  const [envFilePath] = process.argv.slice(2);

  if (!envFilePath) {
    console.error('[ERROR] Missing required argument: <env-file>');
    console.error('Usage: node bin/deploy.mjs <env-file>');
    console.error('Example: node bin/deploy.mjs .env.deploy.myserver');
    process.exit(1);
  }

  const resolvedPath = path.isAbsolute(envFilePath)
    ? envFilePath
    : path.join(process.cwd(), envFilePath);

  console.log('Zotero RAG - Deployment from Environment File');
  console.log('==============================================');
  console.log(`[INFO] Reading: ${resolvedPath}`);

  const { deployOptions, containerEnv } = parseEnvFile(resolvedPath);

  // Auto-add --no-nginx --no-ssl when no FQDN or FQDN is localhost
  const fqdnIdx = deployOptions.findIndex(o => o === '--fqdn');
  const fqdn = fqdnIdx !== -1 ? deployOptions[fqdnIdx + 1] : null;
  if (!fqdn || fqdn === 'localhost' || fqdn === '127.0.0.1') {
    if (!fqdn) console.log('[INFO] No DEPLOY_FQDN set — adding --no-nginx --no-ssl');
    else console.log(`[INFO] DEPLOY_FQDN=${fqdn} is local — adding --no-nginx --no-ssl`);
    if (!deployOptions.includes('--no-nginx')) deployOptions.push('--no-nginx');
    if (!deployOptions.includes('--no-ssl')) deployOptions.push('--no-ssl');
  }

  // Load env file values into process.env so --env KEY picks them up in container.mjs
  dotenvConfig({ path: resolvedPath });

  const cmdParts = ['node', 'bin/container.mjs', 'deploy', ...deployOptions, ...containerEnv];
  const cmd = cmdParts.join(' ');
  console.log(`[INFO] Running: ${cmd}\n`);

  try {
    execSync(cmd, { stdio: 'inherit', cwd: process.cwd(), env: process.env });
  } catch {
    console.error('[ERROR] Deployment failed');
    process.exit(1);
  }
}

main();
