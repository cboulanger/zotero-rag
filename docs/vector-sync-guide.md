# Vector Database Synchronization Guide

## Overview

The Vector Database Synchronization feature allows you to sync your indexed Zotero libraries across multiple devices using remote storage backends (WebDAV or S3). This enables you to:

- Access your vector search index from multiple computers
- Share indexed libraries with team members
- Backup your vector databases to cloud storage
- Search your library without re-indexing on each device

## Architecture

The sync system uses a **snapshot-based approach**:
- Creates compressed archives of your vector database
- Uploads/downloads complete snapshots to/from remote storage
- Tracks library versions to detect which direction to sync
- Handles conflicts when libraries have diverged

## Setup

### 1. Backend Configuration

Sync settings are configured in the backend `.env` file. Copy `.env.dist` to `.env` and configure:

```bash
# Enable sync
SYNC_ENABLED=true

# Choose backend: webdav or s3
SYNC_BACKEND=webdav

# Automatic sync behavior
SYNC_AUTO_PULL=true   # Pull on startup if remote is newer
SYNC_AUTO_PUSH=false  # Push after indexing (opt-in for safety)

# WebDAV Configuration
SYNC_WEBDAV_URL=https://cloud.example.com/remote.php/dav/files/user/
SYNC_WEBDAV_USERNAME=your-username
SYNC_WEBDAV_PASSWORD=your-password
SYNC_WEBDAV_BASE_PATH=/zotero-rag/vectors/

# OR S3 Configuration
SYNC_S3_BUCKET=my-zotero-vectors
SYNC_S3_REGION=us-east-1
SYNC_S3_PREFIX=zotero-rag/vectors/
SYNC_S3_ACCESS_KEY=your-access-key
SYNC_S3_SECRET_KEY=your-secret-key
```

### 2. Storage Backend Options

#### WebDAV (Recommended for Personal Use)

Compatible with:
- **Nextcloud** - Self-hosted or managed
- **ownCloud** - Self-hosted
- **Box** - Commercial cloud storage
- Any WebDAV-compliant server

**Pros:**
- Easy to set up with existing Nextcloud/ownCloud
- Full control over data
- No vendor lock-in

**Cons:**
- Requires WebDAV server setup or subscription
- May have bandwidth/storage limits

#### S3 (Recommended for Team Use)

Compatible with:
- **Amazon S3** - AWS cloud storage
- **MinIO** - Self-hosted S3-compatible
- **DigitalOcean Spaces** - Managed S3-compatible
- **Backblaze B2** - S3-compatible

**Pros:**
- Scalable and reliable
- Global availability
- Versioning and lifecycle policies

**Cons:**
- Requires AWS account or S3-compatible service
- Ongoing storage costs

### 3. Restart Backend

After configuring `.env`, restart the backend server:

```bash
npm run server:restart
```

## Using the Plugin

### Opening the Sync Dialog

There are two ways to access the sync dialog:

1. **Via Menu**: `Tools > Zotero RAG: Sync Vectors...`
2. **Via Preferences**: `Zotero > Settings > Zotero RAG > Open Sync Dialog...`

### Sync Dialog Interface

The sync dialog shows:

- **Sync Configuration Status**: Whether sync is enabled and which backend is active
- **Library List**: All indexed libraries with their sync status
- **Sync Status Badges**:
  - ✓ **In sync** (green) - Local and remote are identical
  - ↓ **Remote ahead** (blue) - Remote version is newer, pull recommended
  - ↑ **Local ahead** (blue) - Local version is newer, push recommended
  - ⚠ **Conflict** (orange) - Libraries have diverged, manual resolution needed
  - ∅ **Not synced** (gray) - No local or remote vectors

### Sync Operations

#### Pull (Download from Remote)

Downloads vectors from remote storage to your local machine.

**When to use:**
- Setting up a new device
- Remote version is newer
- Want to restore from backup

**What happens:**
1. Downloads snapshot from remote
2. Verifies checksum
3. Restores vectors to local database
4. Updates library metadata

#### Push (Upload to Remote)

Uploads your local vectors to remote storage.

**When to use:**
- First time syncing a library
- Local version is newer
- Want to backup current state

**What happens:**
1. Creates snapshot of local vectors
2. Compresses and checksums
3. Uploads to remote storage
4. Updates remote metadata

#### Auto-Sync

Automatically chooses pull or push based on version comparison.

**When to use:**
- Normal sync operations
- Trust the system to choose direction

**What happens:**
- Compares local and remote versions
- Pulls if remote is newer
- Pushes if local is newer
- Shows error if diverged

#### Sync All

Syncs all indexed libraries in one operation.

**When to use:**
- Syncing multiple libraries
- Batch operations

### Conflict Resolution

If libraries have **diverged** (both modified independently), you'll see:
- ⚠ **Conflict** badge
- Two action buttons:
  - **Force Pull** - Overwrite local with remote (lose local changes)
  - **Force Push** - Overwrite remote with local (lose remote changes)

**Important:** Force operations will permanently overwrite one side. Choose carefully!

**Recommendation:**
- If unsure, create a manual backup first
- Pull if you know remote is correct
- Push if you know local is correct
- Re-index if both have valuable data

## Automatic Sync

### Auto-Pull on Startup

When `SYNC_AUTO_PULL=true`:
- Backend checks for remote updates on startup
- Automatically pulls libraries where remote is newer
- Skips libraries that are up-to-date
- Logs all operations

**Use case:** Multi-device workflows where you want the latest vectors available

### Auto-Push After Indexing

When `SYNC_AUTO_PUSH=true`:
- Automatically pushes after successfully indexing a library
- Only pushes if indexing completed without errors
- Continues even if push fails (doesn't block indexing)
- Logs push statistics

**Use case:** Team workflows where you want to immediately share indexed libraries

**Warning:** Only enable if you're confident in your indexing settings. Bad indexes will be pushed automatically.

## Best Practices

### For Single User (Multiple Devices)

```bash
SYNC_ENABLED=true
SYNC_BACKEND=webdav
SYNC_AUTO_PULL=true   # Auto-pull on startup
SYNC_AUTO_PUSH=true   # Auto-push after indexing
```

**Workflow:**
1. Index on Device A → Auto-pushes
2. Start on Device B → Auto-pulls
3. Search on Device B → Uses latest vectors

### For Teams (Shared Libraries)

```bash
SYNC_ENABLED=true
SYNC_BACKEND=s3
SYNC_AUTO_PULL=true   # Everyone gets latest
SYNC_AUTO_PUSH=false  # Manual push to avoid conflicts
```

**Workflow:**
1. Designated person indexes → Manual push via dialog
2. Team members start backend → Auto-pull latest
3. Everyone searches using same vectors

### For Backup Only

```bash
SYNC_ENABLED=true
SYNC_BACKEND=s3
SYNC_AUTO_PULL=false  # Don't auto-restore
SYNC_AUTO_PUSH=false  # Manual backups only
```

**Workflow:**
1. Index normally
2. Periodically open sync dialog
3. Manual push to backup

## Storage Estimates

Snapshot sizes vary by library size:

| Library Size | Items | Chunks  | Snapshot (gzip) | S3 Cost/Month* |
|--------------|-------|---------|-----------------|----------------|
| Small        | 100   | 2,500   | ~15 MB          | $0.35          |
| Medium       | 1,000 | 25,000  | ~150 MB         | $3.45          |
| Large        | 10,000| 250,000 | ~1.5 GB         | $34.50         |

*AWS S3 Standard pricing (~$0.023/GB/month)

**Tips:**
- Compression reduces size by 70-85%
- Vectors compress well
- Use S3 lifecycle policies for old snapshots
- MinIO is free for self-hosted

## Troubleshooting

### Sync Not Enabled

**Problem:** "Sync Not Enabled" message in dialog

**Solution:**
1. Check `.env` file: `SYNC_ENABLED=true`
2. Verify backend credentials are set
3. Restart backend server
4. Refresh sync dialog

### Connection Failed

**Problem:** "Failed to check sync configuration" error

**Solution:**
1. Verify backend is running
2. Check network connectivity
3. Test WebDAV/S3 credentials manually
4. Check firewall settings

### Pull Failed

**Problem:** Download fails or corrupted snapshot

**Solution:**
1. Check available disk space
2. Verify network stability
3. Check remote file exists and is readable
4. Try again (resumable downloads not supported)

### Push Failed

**Problem:** Upload fails

**Solution:**
1. Check remote storage quota
2. Verify write permissions
3. Check network stability
4. Ensure remote directory exists (WebDAV)

### Conflict Detected

**Problem:** "Libraries have diverged" error

**Solution:**
1. Review local and remote versions in dialog
2. Determine which is correct
3. Use "Force Pull" or "Force Push"
4. Or re-index and push fresh

### Slow Syncs

**Problem:** Sync takes too long

**Solution:**
- Use faster internet connection
- Compress more (zstd/xz) - configure in backend
- Reduce library size (index fewer items)
- Use local S3 endpoint (MinIO)

## Security Considerations

### Credentials

- Store credentials in `.env` file (never commit to git)
- Use environment variables on servers
- Rotate passwords periodically
- Use IAM roles for S3 (no keys needed)

### Data Privacy

- All data transmitted over HTTPS/TLS
- Snapshots are unencrypted (contains document text)
- Use S3 server-side encryption for at-rest encryption
- Consider VPN for sensitive data

### Access Control

- Limit WebDAV/S3 credentials to specific paths
- Use separate storage backends for different libraries
- Review access logs periodically
- Revoke access for former team members

## Advanced Topics

### Delta Sync (Future)

Currently not implemented. Full snapshots only.

Future enhancement will support incremental updates for:
- Large libraries (>10k items)
- Frequent updates
- Bandwidth savings

### Multi-Library Batch Sync

Use "Sync All" button to sync multiple libraries at once.

API endpoint also available:
```bash
curl -X POST http://localhost:8119/api/vectors/sync-all?direction=auto
```

### Manual API Usage

For automation or scripts:

```bash
# Check sync status
curl http://localhost:8119/api/vectors/{library_id}/sync-status

# Pull library
curl -X POST http://localhost:8119/api/vectors/{library_id}/pull

# Push library
curl -X POST http://localhost:8119/api/vectors/{library_id}/push

# Auto-sync
curl -X POST http://localhost:8119/api/vectors/{library_id}/sync?direction=auto
```

See [API documentation](http://localhost:8119/docs) for full endpoint details.

## FAQ

**Q: Can I use both WebDAV and S3?**
A: Not simultaneously. Choose one backend at a time.

**Q: Will sync work with Zotero's built-in sync?**
A: Yes, they're independent. Zotero syncs items, we sync vectors.

**Q: Can I sync specific collections instead of whole libraries?**
A: Not yet. Currently library-level only.

**Q: What happens if I delete items in Zotero?**
A: Re-index and push. Vectors for deleted items will be removed.

**Q: Can I sync between different Zotero versions?**
A: Yes, as long as backend/plugin versions match.

**Q: Is there a sync conflict resolution UI?**
A: Currently manual (force pull/push). Future: three-way merge.

**Q: Can I preview what will be synced?**
A: Not yet. Future: diff view showing changes.

**Q: Will syncing affect my Zotero performance?**
A: No. Sync happens outside Zotero, doesn't block UI.

## Support

For issues or questions:
- Check backend logs: `logs/server.log`
- Enable debug mode: Set log level to DEBUG in settings
- Report issues: GitHub repository
- Documentation: See [indexing.md](./indexing.md) for vector database details

## Related Documentation

- [Indexing System](./indexing.md) - How vectors are created
- [Backend Configuration](.env.dist) - All settings
- [API Reference](http://localhost:8119/docs) - REST API endpoints
