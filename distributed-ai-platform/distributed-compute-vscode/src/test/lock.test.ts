import { describe, expect, it } from 'vitest';
import { mkdtempSync, utimesSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { acquireLock } from '../utilities/lock';

describe('setup lock', () => {
  const file = () => join(mkdtempSync(join(tmpdir(), 'dc-lock-')), 'sub', 'setup.lock');

  it('lets only one window hold it, and frees it on release', async () => {
    const f = file();
    const first = await acquireLock(f, 60_000);
    expect(first).toBeDefined();
    expect(await acquireLock(f, 60_000)).toBeUndefined();
    await first!();
    expect(existsSync(f)).toBe(false);
    const again = await acquireLock(f, 60_000);
    expect(again).toBeDefined();
    await again!();
  });

  it('takes over a lock left by a crashed window', async () => {
    const f = file();
    await acquireLock(f, 60_000);
    const old = (Date.now() - 120_000) / 1000;
    utimesSync(f, old, old);
    expect(await acquireLock(f, 60_000)).toBeDefined();
  });
});
