import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { createTar, formatBytes, guessCommand, gzip, isExcluded, packFiles, selectFiles } from './project';

const enc = (s: string) => new TextEncoder().encode(s);

function hasPython(): boolean {
  try {
    execFileSync('python3', ['--version'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

function fakeFile(relativePath: string, text: string): File {
  const file = new File([text], relativePath.split('/').pop() as string);
  Object.defineProperty(file, 'webkitRelativePath', { value: relativePath });
  return file;
}

describe('project folder in the browser', () => {
  it('leaves out the same files as the extension and dc', () => {
    expect(isExcluded('.git/HEAD')).toBe(true);
    expect(isExcluded('src/__pycache__/m.pyc')).toBe(true);
    expect(isExcluded('outputs/model.pt')).toBe(true);
    expect(isExcluded('train.py')).toBe(false);
    expect(isExcluded('configs/outputs.yaml')).toBe(false);
  });

  it('takes paths relative to the chosen folder', () => {
    const picked = selectFiles([
      fakeFile('proj/train.py', 'print(1)'),
      fakeFile('proj/src/model.py', 'X=1'),
      fakeFile('proj/.venv/bin/python', 'x'),
      fakeFile('proj/outputs/old.bin', 'x'),
    ]);
    expect(picked.map((p) => p.path)).toEqual(['src/model.py', 'train.py']);
    expect(guessCommand(picked.map((p) => p.path))).toBe('python3 train.py');
  });

  it('writes a tar with 512-byte records and long paths in the prefix field', () => {
    const tar = createTar([
      { path: 'b.txt', data: enc('bbb') },
      { path: 'a.sh', data: enc('#!/bin/sh\n') },
      { path: `${'dir/'.repeat(30)}x.txt`, data: enc('deep') },
    ]);
    expect(tar.length % 512).toBe(0);
    const name = new TextDecoder().decode(tar.subarray(0, 100)).replace(/\0+$/, '');
    expect(name).toBe('a.sh'); // sorted
    expect(new TextDecoder().decode(tar.subarray(100, 107))).toBe('0000755');
  });

  it.skipIf(!hasPython())('produces an archive Python tarfile reads (as the master does)', async () => {
    const files = [
      { path: 'train.py', data: enc('print("hi")\n') },
      { path: `${'dir/'.repeat(30)}x.txt`, data: enc('deep') },
    ];
    const archive = await gzip(createTar(files));
    const dir = mkdtempSync(join(tmpdir(), 'dc-fe-'));
    writeFileSync(join(dir, 'p.tar.gz'), archive);
    const out = execFileSync('python3', ['-c',
      'import json,sys,tarfile\nt=tarfile.open(sys.argv[1],"r:gz")\nprint(json.dumps({m.name:t.extractfile(m).read().decode() for m in t}))',
      join(dir, 'p.tar.gz')]).toString();
    expect(JSON.parse(out)).toEqual({ 'train.py': 'print("hi")\n', [`${'dir/'.repeat(30)}x.txt`]: 'deep' });
  });

  it('packs selected files and refuses huge folders', async () => {
    const { archive, bytes } = await packFiles([{ file: fakeFile('p/a.py', 'abc'), path: 'a.py' }]);
    expect(bytes).toBe(3);
    expect([archive[0], archive[1]]).toEqual([0x1f, 0x8b]);
    const big = { file: { size: 600 * 1024 * 1024 } as File, path: 'big.bin' };
    await expect(packFiles([big])).rejects.toThrow(/500 MB/);
    expect(formatBytes(2048)).toBe('2.0 KB');
  });
});
