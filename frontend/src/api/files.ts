import { pollGet } from './client'
import { ossRedirectUrl } from '../lib/ossLink'

export interface FileTouch {
  id: number
  path: string
  tool: string
  ts: string | null
}

/** Terminal file-link + Recent-files endpoints.
 *
 * `preview` and `oss` are pure URL builders — the actual navigation
 * happens via `window.open(url, '_blank')` in the xterm link handler
 * (independent-window UX per user's decision), not via the axios
 * client. `recent` is the only endpoint we actually fetch through.
 */
export const filesApi = {
  // Pass `sid` when `path` may be relative — the backend will resolve
  // against that session's cwd (with a traversal guard). Absolute paths
  // ignore sid, so it's safe to always thread when known.
  previewUrl: (path: string, sid?: string | null): string => {
    const base = `/api/files/preview?path=${encodeURIComponent(path)}`
    return sid ? `${base}&session_id=${encodeURIComponent(sid)}` : base
  },
  // Single definition, shared with the markdown renderer — the terminal
  // matcher and a chat bubble must produce the same href for the same URI.
  ossRedirectUrl,
  recent: async (sid: string, limit = 50): Promise<{ count: number; items: FileTouch[] }> => {
    // pollGet: loaded on session open — fast-fail 8s + fresh-connection retry so
    // a wedged tunnel connection doesn't hang the connect (see client.ts).
    const { data } = await pollGet<{ count: number; items: FileTouch[] }>(
      `/api/files/recent/${sid}`,
      { params: { limit } },
    )
    return data
  },
}
