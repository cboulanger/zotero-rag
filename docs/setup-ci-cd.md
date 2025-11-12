# CI/CD Quick Start

**Quick setup guide for new contributors.** For complete documentation, see [ci-cd.md](./ci-cd.md).

## Setup (One-Time)

```bash
# 1. Install dependencies
npm install

# 2. Setup commit message validation hook
python scripts/setup_hooks.py
```

Done! You're ready to commit.

## Daily Usage

### Making Commits

```bash
# Option 1: Interactive helper (recommended for first-timers)
npm run commit

# Option 2: Manual (once you know the format)
git commit -m "feat: add new feature"
git commit -m "fix: resolve bug"
git commit -m "docs: update documentation"
```

### Pushing Changes

```bash
git push origin main
```

That's it! If your commit includes `feat:` or `fix:`, a release is created automatically.

## Commit Format

```
<type>(<scope>): <subject>
```

### Common Types

| Type | When to Use | Version Bump |
|------|-------------|--------------|
| `feat` | New features | Minor (0.1.0 → 0.2.0) |
| `fix` | Bug fixes | Patch (0.1.0 → 0.1.1) |
| `docs` | Documentation | None |
| `test` | Tests | None |
| `refactor` | Code refactoring | None |
| `chore` | Maintenance | None |

For breaking changes, add `!` after type: `feat!: redesign API`

**[See all commit types →](./ci-cd.md#commit-types)**

## Examples

```bash
feat: add query caching
fix(api): resolve timeout issue
docs: update installation guide
feat!: redesign plugin API
```

**[See more examples →](./ci-cd.md#examples)**

## Validation

Every commit is validated automatically. Invalid commits are rejected:

```
[ERROR] Invalid commit message
Expected format: <type>(<scope>): <subject>
Examples:
  feat: add new feature
  fix(api): resolve crash
```

**[Troubleshooting validation →](./ci-cd.md#commit-rejected-by-hook)**

## Quick Reference

| Command | Description |
|---------|-------------|
| `npm run commit` | Interactive commit helper |
| `git push origin main` | Push and trigger release |
| `npm run test:backend` | Run tests locally |

**[Full command reference →](./ci-cd.md#npm-scripts-reference)**

## Common Questions

**Q: Why was my commit rejected?**
A: Check the error message. Most common issues:
- Missing colon: `feat add feature` → `feat: add feature`
- Uppercase subject: `feat: Add feature` → `feat: add feature`
- Period at end: `feat: add feature.` → `feat: add feature`

**[More troubleshooting →](./ci-cd.md#troubleshooting)**

**Q: I pushed to main but no release was created. Why?**
A: Only `feat:`, `fix:`, and `perf:` commits trigger releases. If you only have `docs:` or `chore:` commits, no release is created (this is correct).

**[Understanding release triggers →](./ci-cd.md#what-triggers-a-release)**

**Q: How do I bypass the hook in an emergency?**
A: `git commit --no-verify -m "emergency fix"` (not recommended)

**[Emergency procedures →](./ci-cd.md#bypass-hook-for-emergency)**

## Complete Documentation

For detailed information, see:
- **[ci-cd.md](./ci-cd.md)** - Complete CI/CD documentation
  - How semantic-release works
  - All commit types and rules
  - Configuration files explained
  - Best practices
  - Advanced usage (pre-releases, etc.)
  - Security considerations

## Support

- [Conventional Commits Specification](https://www.conventionalcommits.org/)
- [Semantic Versioning](https://semver.org/)
