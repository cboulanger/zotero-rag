# CI/CD Workflow Documentation

This document describes the continuous integration and deployment workflow for the Zotero RAG project using **semantic-release** for fully automated releases.

## Overview

The project uses **GitHub Actions** with **semantic-release** for automated testing, versioning, building, and releasing. Releases are triggered automatically based on commit messages following the Conventional Commits specification.

## Key Principles

1. **Automated Versioning** - Version numbers are determined automatically from commit messages
2. **Conventional Commits** - Strict commit message format enforced by Git hooks
3. **Zero Manual Steps** - Push to main → automatic release (if relevant changes)
4. **Dynamic Tags** - `latest` and `stable` tags always point to newest release

## Architecture

### Branches

- **`main`** - Production branch, protected, releases triggered on push
- **`develop`** - Optional development branch for integration (not required)
- **Feature branches** - Short-lived branches, merge to main via PR

### Tags

1. **Version tags** (`v1.2.3`) - Created automatically by semantic-release
2. **Dynamic tags** - Updated automatically:
   - `latest` - Points to the most recent stable release
   - `stable` - Alias for `latest` (production-ready)

### Workflows

#### 1. CI Workflow (`.github/workflows/ci.yml`)

**Triggers:**

- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`

**Jobs:**

- **test** - Runs unit tests with pytest
- **lint** - Code quality checks (optional, non-blocking)
- **build-plugin** - Builds XPI and uploads as artifact (7-day retention)

**Purpose:** Ensures code quality on every commit and PR.

#### 2. Release Workflow (`.github/workflows/release.yml`)

**Triggers:**

- Push to `main` branch

**Jobs:**

- **test** - Runs unit tests (must pass)
- **release** - semantic-release analyzes commits, determines version, builds XPI, creates release
- **update-tags** - Updates `latest` and `stable` dynamic tags

**Purpose:** Fully automated release creation.

## Commit Message Format

### Structure

Every commit message must follow this format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Required:**

- `<type>`: Type of change (see below)
- `<subject>`: Short description (lowercase, no period, max 100 chars)

**Optional:**

- `<scope>`: Area of codebase affected (e.g., `api`, `plugin`, `docs`)
- `<body>`: Detailed description
- `<footer>`: Breaking changes, issue references

### Commit Types

| Type | Description | Version Bump | Example |
|------|-------------|--------------|---------|
| `feat` | New feature | **Minor** (0.1.0 → 0.2.0) | `feat: add RAG query caching` |
| `fix` | Bug fix | **Patch** (0.1.0 → 0.1.1) | `fix: resolve memory leak in embeddings` |
| `docs` | Documentation only | None | `docs: update installation guide` |
| `style` | Code style/formatting | None | `style: format with black` |
| `refactor` | Code refactoring | None | `refactor: simplify query engine` |
| `perf` | Performance improvement | **Patch** | `perf: optimize vector search` |
| `test` | Add/update tests | None | `test: add integration tests` |
| `build` | Build system/dependencies | None | `build: update dependencies` |
| `ci` | CI/CD changes | None | `ci: add coverage reporting` |
| `chore` | Other changes | None | `chore: update gitignore` |
| `revert` | Revert previous commit | Based on reverted commit | `revert: feat: add feature X` |

### Breaking Changes

To trigger a **major** version bump (0.1.0 → 1.0.0), use `!` after type or add `BREAKING CHANGE:` footer:

```bash
# Method 1: Exclamation mark
feat!: redesign API endpoints

# Method 2: Footer
feat: redesign API endpoints

BREAKING CHANGE: API endpoints now use /v2/ prefix
```

### Examples

```bash
# Patch release (0.1.0 → 0.1.1)
fix: correct PDF extraction error
fix(backend): resolve Qdrant connection timeout
perf: improve chunking performance

# Minor release (0.1.0 → 0.2.0)
feat: add multi-library query support
feat(plugin): add progress bar for indexing

# Major release (0.1.0 → 1.0.0)
feat!: redesign plugin API
fix!: change configuration file format

BREAKING CHANGE: Config format changed to YAML
```

## Release Process

### Fully Automated Workflow

```
Developer commits with conventional format
    ↓
Push to main (or merge PR)
    ↓
GitHub Actions: Run tests
    ↓
semantic-release analyzes commits since last release
    ↓
Determines version bump (major/minor/patch)
    ↓
Updates version files (package.json, pyproject.toml, etc.)
    ↓
Generates CHANGELOG.md
    ↓
Creates git tag (e.g., v1.2.3)
    ↓
Builds plugin XPI
    ↓
Creates GitHub Release with XPI attached
    ↓
Updates 'latest' and 'stable' tags
```

**Zero manual intervention required!**

### Step-by-Step Usage

#### 1. Make Your Changes

```bash
# Create feature branch (optional but recommended)
git checkout -b feat/add-caching

# Make code changes
# ... edit files ...

# Test locally
npm run test:backend
```

#### 2. Commit with Conventional Format

**Option A: Use commitizen (recommended)**

```bash
npm run commit
# Interactive prompt guides you through creating valid commit message
```

**Option B: Manual commit**

```bash
git add .
git commit -m "feat: add query result caching"
```

The Git hook will validate your message automatically. If invalid, commit is rejected with helpful error message.

#### 3. Push to Main

```bash
# If on feature branch, push and create PR
git push origin feat/add-caching
# Then merge PR through GitHub UI

# If on main (for small changes)
git push origin main
```

#### 4. Automatic Release

GitHub Actions will:

1. Run tests
2. Analyze your commits
3. Determine if release is needed
4. If yes: bump version, create release, publish XPI

**That's it!** No manual version bumps, no manual tags, no manual releases.

### What Triggers a Release?

semantic-release creates a new release if commits since last release include:

- ✅ `feat:` commits (minor bump)
- ✅ `fix:` commits (patch bump)
- ✅ `perf:` commits (patch bump)
- ✅ `feat!:` or `BREAKING CHANGE:` (major bump)

No release is created for:

- ❌ `docs:` commits
- ❌ `style:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:` commits
- ❌ Commits with `[skip ci]` or `[skip release]` in message

## Setup Instructions

### First-Time Setup

```bash
# Install Node dependencies
npm install

# Setup Git hooks for commit message validation
python scripts/setup_hooks.py

# OR use npm (if using Husky)
npm run prepare
```

### Commit Message Validation

After setup, every commit is validated automatically. If invalid:

```
[ERROR] Invalid commit message
======================================================================

Your message:
  Added new feature

Invalid commit message format.

Expected format:
  <type>(<scope>): <subject>

Valid types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert

Examples:
  feat: add new feature
  fix(api): resolve crash on startup
  docs: update README
  feat!: breaking API change

Commit aborted. Please fix your commit message and try again.
======================================================================
```

### Testing Commit Messages

```bash
# Test validation without committing
python scripts/validate_commit_msg.py --message "feat: add new feature"
# Output: [VALID] Valid commit message

python scripts/validate_commit_msg.py --message "Added new feature"
# Output: [ERROR] Invalid commit message format.
```

## Version Files

semantic-release automatically updates these files:

| File | Format | Purpose |
|------|--------|---------|
| `package.json` | `"version": "1.0.0"` | npm package version (source) |
| `pyproject.toml` | `version = "1.0.0"` | Python package version |
| `plugin/manifest.json` | `"version": "1.0.0"` | Zotero plugin version (shown in UI) |
| `backend/__version__.py` | `__version__ = "1.0.0"` | Python runtime version |
| `CHANGELOG.md` | Markdown | Auto-generated release notes |

All files are updated atomically by the `scripts/version.py` script.

## Using Dynamic Tags

### For Documentation Links

Use dynamic tags to create stable documentation links:

```markdown
<!-- Always points to latest stable release -->
Download: https://github.com/USERNAME/zotero-rag/releases/download/stable/zotero-rag-X.Y.Z.xpi

<!-- Or use 'latest' -->
Download: https://github.com/USERNAME/zotero-rag/releases/latest/download/zotero-rag-X.Y.Z.xpi
```

### For Zotero Update Manifest

Create `updates.json` for automatic plugin updates:

```json
{
  "addons": {
    "zotero-rag@example.com": {
      "updates": [
        {
          "version": "latest",
          "update_link": "https://github.com/USERNAME/zotero-rag/releases/latest/download/zotero-rag-X.Y.Z.xpi",
          "applications": {
            "zotero": {
              "strict_min_version": "7.0"
            }
          }
        }
      ]
    }
  }
}
```

## Troubleshooting

### Commit Rejected by Hook

If your commit is rejected:

1. Read the error message carefully
2. Fix the commit message format
3. Try again: `git commit --amend` or `npm run commit`

Common issues:

- ❌ `Added new feature` → ✅ `feat: add new feature`
- ❌ `fix: Fixed bug.` → ✅ `fix: resolve bug in API`
- ❌ `Feat: Add feature` → ✅ `feat: add feature` (lowercase subject)

### No Release Created

If you pushed to main but no release was created:

**Check if commits trigger releases:**

```bash
git log --oneline
# Look for feat:, fix:, perf: commits
```

Only these types trigger releases. If you only have `docs:` or `chore:` commits, no release is created (this is correct behavior).

**Check GitHub Actions:**

1. Go to Actions tab
2. Look for "Release" workflow
3. Check logs for semantic-release output

### Tests Fail in CI

If tests fail:

1. Release is **not created** (workflow stops)
2. Fix the failing tests locally
3. Commit fix: `fix: resolve test failure`
4. Push again

### Bypass Hook for Emergency

**Not recommended**, but if absolutely necessary:

```bash
git commit --no-verify -m "emergency fix"
```

This skips the commit message hook but may cause semantic-release to skip the commit.

### Manual Release Trigger

If semantic-release fails to detect changes, you can manually trigger a release:

```bash
# Force patch release
git commit --allow-empty -m "chore: trigger release [skip ci]"
git commit --allow-empty -m "fix: force patch release"
git push
```

## NPM Scripts Reference

| Command | Description |
|---------|-------------|
| `npm run commit` | Interactive commit helper (commitizen) |
| `npm run semantic-release` | Run semantic-release locally (testing only) |
| `npm run test:backend` | Run unit tests |
| `npm run plugin:build` | Build plugin XPI |

## Configuration Files

### `.releaserc.json`

Configures semantic-release behavior:

- Plugins for changelog generation, version updates, GitHub releases
- Asset uploads (XPI file)
- Commit message parsing rules

### `.commitlintrc.json`

Configures commit message validation rules:

- Valid commit types
- Message format requirements
- Length limits

### `.husky/commit-msg` (or `scripts/setup_hooks.py`)

Git hook that validates commit messages before commit is created.

## Best Practices

### Writing Commits

✅ **Do:**

- Use imperative mood: "add feature" not "added feature"
- Keep subject under 100 characters
- Use lowercase for subject
- Omit period at end of subject
- Add body for complex changes
- Reference issues: "fix: resolve #123"

❌ **Don't:**

- Mix multiple unrelated changes in one commit
- Use vague subjects: "fix stuff", "update code"
- Start subject with uppercase
- End subject with period

### When to Use Each Type

**feat** - User-facing features:

```bash
feat: add RAG query caching
feat(plugin): add keyboard shortcuts
```

**fix** - Bug fixes:

```bash
fix: resolve PDF extraction crash
fix(api): correct response status codes
```

**docs** - Documentation only:

```bash
docs: update README with installation steps
docs(api): add endpoint examples
```

**refactor** - Internal improvements:

```bash
refactor: simplify query engine logic
refactor(db): optimize vector search
```

**perf** - Performance improvements:

```bash
perf: reduce memory usage in embeddings
perf(backend): cache frequently accessed data
```

**test** - Test changes:

```bash
test: add integration tests for RAG pipeline
test(api): add endpoint validation tests
```

**chore** - Maintenance:

```bash
chore: update dependencies
chore: clean up old debug code
```

### Breaking Changes

Only use breaking changes for actual breaking changes:

✅ **Breaking:**

- Changed API endpoints
- Removed features
- Changed configuration format
- Incompatible plugin updates

❌ **Not Breaking:**

- Internal refactoring
- Performance improvements
- Bug fixes (even if behavior changes slightly)

## Migrating from Manual Releases

If you were using the old manual release process:

### Old Process (Removed)

```bash
npm run release:patch  # ❌ Removed
npm run release:push   # ❌ Removed
```

### New Process

```bash
git commit -m "feat: add new feature"
git push origin main
# Release happens automatically!
```

## Security

### Required Permissions

GitHub Actions workflow requires:

- `contents: write` - Create releases, push tags
- `issues: write` - Comment on issues (optional)
- `pull-requests: write` - Comment on PRs (optional)

### Built-in Tokens

Uses `GITHUB_TOKEN` (automatic, no setup needed):

- Automatically provided by GitHub Actions
- Scoped to repository
- No additional secrets required

## Advanced Usage

### Pre-release Versions

To create pre-releases (alpha, beta, rc):

```bash
# Push to beta branch (configure in .releaserc.json)
git push origin beta
# Creates: v1.0.0-beta.1
```

### Skip CI/Release

```bash
# Skip CI entirely
git commit -m "docs: update README [skip ci]"

# Run CI but skip release
git commit -m "refactor: cleanup code [skip release]"
```

### Dry Run

Test semantic-release locally without publishing:

```bash
npx semantic-release --dry-run
```

## References

- [Conventional Commits](https://www.conventionalcommits.org/)
- [semantic-release Documentation](https://semantic-release.gitbook.io/)
- [Commitizen](https://github.com/commitizen/cz-cli)
- [Commitlint](https://commitlint.js.org/)
- [Semantic Versioning](https://semver.org/)
