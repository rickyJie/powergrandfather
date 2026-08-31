<script setup lang="ts">
/**
 * BackupPanel — create / list / download / delete CSM state bundles.
 * Restore is documented but not executed here (SQLite live-restore
 * would fight the running process; the shell script handles it).
 */
import { apiErrorMessage } from '../lib/apiError'
import { onMounted, ref } from 'vue'
import { backupApi, type BackupEntry } from '../api/backup'
import { useToast } from '../composables/useToast'

const toast = useToast()

const entries = ref<BackupEntry[]>([])
const totalBytes = ref<number>(0)
const backupDir = ref<string>('')
const maxBackups = ref<number>(20)

const note = ref<string>('')
const creating = ref(false)
const loading = ref(false)
const errorMsg = ref<string>('')

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const r = await backupApi.list()
    entries.value = r.backups
    totalBytes.value = r.total_bytes
    backupDir.value = r.backup_dir
    maxBackups.value = r.max_backups
  } catch (e) {
    errorMsg.value = apiErrorMessage(e)
  } finally {
    loading.value = false
  }
}

async function createBackup() {
  creating.value = true
  try {
    const r = await backupApi.create(note.value)
    toast.success(`Backup created: ${r.name} (${fmtBytes(r.size_bytes)})`)
    note.value = ''
    await load()
  } catch (e) {
    toast.error(`Backup failed: ${apiErrorMessage(e)}`)
  } finally {
    creating.value = false
  }
}

async function deleteBackup(name: string) {
  if (!confirm(`Delete backup ${name}? This cannot be undone.`)) return
  try {
    await backupApi.remove(name)
    toast.success('Backup deleted')
    await load()
  } catch (e) {
    toast.error(`Delete failed: ${apiErrorMessage(e)}`)
  }
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(2)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function fmtWhen(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

onMounted(load)
</script>

<template>
  <section class="card">
    <div class="card-head">
      <div class="kicker">Backup</div>
      <h2>Create a snapshot</h2>
    </div>
    <p class="card-desc">
      Bundles the SQLite database (via the online backup API so it's
      safe to run while CSM is live), all workflow YAMLs under
      <code>tasks/</code>, and current Alembic migration scripts into a
      single <code>.tar.gz</code>. Environment variables and OS-level
      configuration are <b>not</b> included — record those separately.
    </p>
    <div class="bp-create">
      <input
        v-model="note"
        class="bp-note-input"
        type="text"
        placeholder="Optional note (e.g. 'pre-refactor snapshot')"
        maxlength="500"
      />
      <button class="bp-primary" :disabled="creating" @click="createBackup">
        {{ creating ? 'Creating…' : 'Create backup' }}
      </button>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="kicker">Backup</div>
      <h2>Existing snapshots</h2>
    </div>
    <p class="card-desc bp-meta">
      <code>{{ backupDir }}</code>
      · {{ entries.length }} / {{ maxBackups }} backups
      · {{ fmtBytes(totalBytes) }} used on disk
    </p>
    <div v-if="errorMsg" class="bp-error">{{ errorMsg }}</div>
    <div v-if="loading" class="bp-muted">Loading…</div>
    <div v-else-if="!entries.length" class="bp-muted">No backups yet.</div>
    <table v-else class="bp-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Created</th>
          <th class="bp-num">Size</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in entries" :key="b.name">
          <td><code>{{ b.name }}</code></td>
          <td>{{ fmtWhen(b.created_at) }}</td>
          <td class="bp-num">{{ fmtBytes(b.size_bytes) }}</td>
          <td class="bp-actions">
            <a class="bp-link" :href="`/api/backup/download/${encodeURIComponent(b.name)}`" download>
              Download
            </a>
            <button class="bp-danger" @click="deleteBackup(b.name)">Delete</button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="card">
    <div class="card-head">
      <div class="kicker">Backup</div>
      <h2>Restore</h2>
    </div>
    <p class="card-desc">
      Restore is a shell operation, not an HTTP action — while CSM is
      running, SQLite holds the WAL and can't be safely overwritten.
      To restore into this project root:
    </p>
    <pre class="bp-code">./scripts/stop.sh
./scripts/restore_backup.sh backups/csm-backup-YYYYMMDD-HHMMSS.tar.gz
conda activate csm
alembic upgrade head
./scripts/start.sh</pre>
    <p class="card-desc bp-note">
      The script prompts before overwriting; export
      <code>CSM_RESTORE_YES=1</code> to skip the prompt in scripted
      workflows, or <code>CSM_RESTORE_KEEP_TASKS=1</code> to restore
      the database only.
    </p>
  </section>
</template>

<style scoped>
.bp-create {
  display: flex; gap: 8px; align-items: center;
}
.bp-note-input {
  flex: 1;
  padding: 6px 10px; border: 1px solid var(--border, #cbd5e1);
  border-radius: 4px; font-size: 13px;
}
.bp-primary {
  padding: 6px 14px; font-size: 13px;
  border: 1px solid #bfdbfe; background: transparent; color: #1e40af;
  border-radius: 4px; cursor: pointer;
}
.bp-primary:hover:not(:disabled) { background: #eff6ff; }
.bp-primary:disabled { opacity: 0.5; cursor: not-allowed; }

.bp-meta { font-size: 12px; color: var(--ink-mute, #64748b); }
.bp-muted { color: var(--ink-mute, #94a3b8); font-size: 13px; padding: 8px 0; }
.bp-error {
  padding: 8px 12px; background: #fef2f2; color: #991b1b;
  border-radius: 4px; margin-bottom: 8px; font-size: 12.5px;
}

.bp-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
}
.bp-table th, .bp-table td {
  text-align: left; padding: 6px 10px;
  border-bottom: 1px solid var(--border, #e2e8f0);
}
.bp-table th {
  font-weight: 600; color: var(--ink-mute); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.bp-table code {
  background: rgba(0,0,0,0.05); padding: 1px 6px; border-radius: 3px;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12px;
}
.bp-num { text-align: right; }
.bp-actions { text-align: right; display: flex; gap: 8px; justify-content: flex-end; }

.bp-link {
  padding: 3px 10px; font-size: 12px; color: #2563eb;
  border: 1px solid #bfdbfe; background: #fff; border-radius: 3px;
  text-decoration: none;
}
.bp-link:hover { background: #eff6ff; }
.bp-danger {
  padding: 3px 10px; font-size: 12px;
  border: 1px solid #fecaca; background: #fff; color: #991b1b;
  border-radius: 3px; cursor: pointer;
}
.bp-danger:hover { background: #fef2f2; }

.bp-code {
  padding: 10px 12px; background: #0f172a; color: #e2e8f0;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12.5px;
  border-radius: 4px; white-space: pre-wrap;
}
.bp-note { margin-top: 8px; font-size: 12px; }
</style>
