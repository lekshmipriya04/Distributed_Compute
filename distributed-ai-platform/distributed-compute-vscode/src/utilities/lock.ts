// A lock file shared by every VS Code window on this machine, so two windows
// opening at once do not both run `docker compose up` (which leaves containers
// with no network).

import { mkdir, open, stat, unlink } from 'node:fs/promises';
import * as path from 'node:path';

/**
 * Take the lock, or return undefined when another live holder has it. A lock
 * older than `staleMs` is from a window that crashed and is taken over.
 * The returned function releases it.
 */
export async function acquireLock(file: string, staleMs: number): Promise<(() => Promise<void>) | undefined> {
  await mkdir(path.dirname(file), { recursive: true });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const handle = await open(file, 'wx');
      await handle.writeFile(String(process.pid));
      await handle.close();
      return () => unlink(file).catch(() => undefined);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'EEXIST') {
        throw err;
      }
      const age = await stat(file).then(
        (s) => Date.now() - s.mtimeMs,
        () => Infinity, // removed in between: try again
      );
      if (age < staleMs) {
        return undefined;
      }
      await unlink(file).catch(() => undefined);
    }
  }
  return undefined;
}
