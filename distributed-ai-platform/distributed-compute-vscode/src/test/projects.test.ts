import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_EXCLUDES,
  createTarGz,
  formatBytes,
  guessCommand,
  isExcluded,
  isSafeArchivePath,
  readTarGz,
  sha256Hex,
  validateProjectBlock,
} from '../models/project';
import { validateConfig } from '../configuration/projectConfig';
import { executionLabel } from '../models/execution';
import { formatTaskLog } from '../models/onboarding';
import { GatewayApi } from '../api/gateway';
import { ApiClient } from '../api/client';

const enc = (s: string) => new TextEncoder().encode(s);
const dec = (b: Uint8Array) => new TextDecoder().decode(b);

function hasPython(): boolean {
  try {
    execFileSync('python3', ['--version'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

describe('which files go into a project', () => {
  it('leaves out version control, caches, environments and outputs at any depth', () => {
    for (const path of ['.git/config', 'node_modules/x/index.js', 'src/__pycache__/a.pyc', '.venv/bin/python',
      'pkg/venv/lib/x.py', 'outputs/model.pt', 'distributed-results/abc/task-001/x', 'a/b/c.pyc', 'sub/.DS_Store']) {
      expect(isExcluded(path, DEFAULT_EXCLUDES), path).toBe(true);
    }
    for (const path of ['train.py', 'src/model.py', 'requirements.txt', 'configs/outputs.yaml', 'data/train.csv',
      'venv.md', 'myoutputs/x']) {
      expect(isExcluded(path, DEFAULT_EXCLUDES), path).toBe(false);
    }
  });

  it('supports extra patterns from distributed.json', () => {
    const patterns = ['data/raw/', '*.ckpt', 'docs/**/*.png', '/notes.txt'];
    expect(isExcluded('data/raw/a.csv', patterns)).toBe(true);
    expect(isExcluded('data/clean.csv', patterns)).toBe(false);
    expect(isExcluded('models/best.ckpt', patterns)).toBe(true);
    expect(isExcluded('docs/a/b/c.png', patterns)).toBe(true);
    expect(isExcluded('docs/readme.md', patterns)).toBe(false);
    expect(isExcluded('notes.txt', patterns)).toBe(true);
    expect(isExcluded('sub/notes.txt', patterns)).toBe(false);
  });

  it('guesses a command from the files', () => {
    expect(guessCommand(['src/x.py', 'train.py', 'main.py'])).toBe('python3 train.py');
    expect(guessCommand(['main.py'])).toBe('python3 main.py');
    expect(guessCommand(['run.sh', 'README.md'])).toBe('sh run.sh');
    expect(guessCommand(['Makefile'])).toBe('make');
    expect(guessCommand(['tool.py'])).toBe('python3 tool.py');
    expect(guessCommand(['README.md'])).toBe('');
  });
});

describe('project archives', () => {
  const files = [
    { path: 'train.py', data: enc('print("hi")\n') },
    { path: 'src/model.py', data: enc('X = 1\n') },
    { path: 'run.sh', data: enc('#!/bin/sh\necho ok\n'), mode: 0o755 },
    { path: `${'deep/'.repeat(30)}file.txt`, data: enc('long path') },
  ];

  it('round-trips and is identical for the same files (so it is uploaded once)', () => {
    const a = createTarGz(files);
    const b = createTarGz([...files].reverse());
    expect(sha256Hex(a)).toBe(sha256Hex(b));
    const back = readTarGz(a);
    expect(back.map((e) => e.path)).toEqual([...files.map((f) => f.path)].sort());
    expect(dec(back.find((e) => e.path === 'train.py')!.data)).toBe('print("hi")\n');
    expect(back.find((e) => e.path === 'run.sh')!.mode).toBe(0o755);
    expect(sha256Hex(createTarGz([{ path: 'train.py', data: enc('changed') }]))).not.toBe(sha256Hex(a));
  });

  it.skipIf(!hasPython())('is read by Python tarfile exactly as the master does', () => {
    const dir = mkdtempSync(join(tmpdir(), 'dc-tar-'));
    const archive = join(dir, 'p.tar.gz');
    writeFileSync(archive, createTarGz(files));
    const out = execFileSync('python3', ['-c',
      'import json,sys,tarfile\n' +
      't=tarfile.open(sys.argv[1],"r:gz")\n' +
      'print(json.dumps({m.name:[m.size,oct(m.mode),t.extractfile(m).read().decode()] for m in t}))',
      archive]).toString();
    const members = JSON.parse(out) as Record<string, [number, string, string]>;
    expect(members['src/model.py']).toEqual([6, '0o644', 'X = 1\n']);
    expect(members['run.sh'][1]).toBe('0o755');
    expect(members[`${'deep/'.repeat(30)}file.txt`][2]).toBe('long path');
  });

  it.skipIf(!hasPython())('reads the outputs archive Python writes', () => {
    const dir = mkdtempSync(join(tmpdir(), 'dc-tar-'));
    const archive = join(dir, 'o.tar.gz');
    execFileSync('python3', ['-c',
      'import io,sys,tarfile\n' +
      't=tarfile.open(sys.argv[1],"w:gz")\n' +
      'for n,d in [("outputs/metrics.json",b"{}"),("outputs/" + "x"*120 + "/model.bin",b"\\x00\\x01")]:\n' +
      '  i=tarfile.TarInfo(n); i.size=len(d); t.addfile(i,io.BytesIO(d))\n' +
      't.close()', archive]);
    const entries = readTarGz(new Uint8Array(readFileSync(archive)));
    expect(entries.map((e) => e.path)).toEqual(['outputs/metrics.json', `outputs/${'x'.repeat(120)}/model.bin`]);
    expect([...entries[1].data]).toEqual([0, 1]);
  });

  it('refuses paths that leave the folder', () => {
    expect(isSafeArchivePath('outputs/a.txt')).toBe(true);
    for (const bad of ['../x', 'a/../../x', '/etc/passwd', 'C:\\x', '\\\\server\\x', '']) {
      expect(isSafeArchivePath(bad), bad).toBe(false);
    }
    const evil = createTarGz([{ path: '../evil.txt', data: enc('x') }]);
    expect(() => readTarGz(evil)).toThrow(/Unsafe path/);
  });

  it('formats sizes', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(250 * 1024 * 1024)).toBe('250 MB');
  });
});

describe('distributed.json project block', () => {
  it('accepts a project-only config and checks its fields', () => {
    expect(validateConfig({ version: 1, name: 'p', project: { command: 'python train.py' } }).ok).toBe(true);
    expect(validateProjectBlock({ command: 'make', requirements: 'requirements.txt', outputs: ['outputs', 'models'],
      exclude: ['data/raw/'], datasets: [{ dataset: 'abc', path: 'data/train.csv' }] })).toEqual([]);
    expect(validateProjectBlock({ command: ' ' })).toHaveLength(1);
    expect(validateProjectBlock({ command: 'x', outputs: ['../up'] })).toHaveLength(1);
    expect(validateProjectBlock({ command: 'x', requirements: '/etc/passwd' })).toHaveLength(1);
    expect(validateProjectBlock({ command: 'x', datasets: [{ path: 'a' }] })).toHaveLength(1);
    expect(validateConfig({ version: 1, name: 'p', project: { command: '' } }).ok).toBe(false);
    // A file config still needs its entrypoint and runtime.
    expect(validateConfig({ version: 1, name: 'p' }).ok).toBe(false);
  });
});

describe('project runs in the views', () => {
  it('names a run by its command and shows output files per task', () => {
    expect(executionLabel({ runtime: 'project', total: 4, project: { bundleId: 'a', command: 'python train.py --epochs 3' } }))
      .toBe('python train.py --epochs 3 · 4 tasks');
    expect(executionLabel({ runtime: 'project', total: 1, project: { bundleId: 'a', command: 'x'.repeat(60) } }))
      .toBe(`${'x'.repeat(39)}… · 1 task`);
    const log = formatTaskLog({
      batch: { id: 'b1', runtime: 'project', status: 'completed', total: 1, project: { command: 'python train.py' } },
      tasks: [{ id: 't1', seq: 0, status: 'completed', exit_code: 0, attempts: 1, stdout: 'done', output_files: 3 }],
    });
    expect(log).toContain('Project · python train.py · completed');
    expect(log).toContain('━━ Task 1 · completed · exit 0 · not scheduled yet · 3 output files');
  });
});

describe('gateway project calls', () => {
  function api(handler: (url: string, init: RequestInit) => Response) {
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => handler(url, init));
    return { gw: new GatewayApi(new ApiClient({ baseUrl: 'http://m:8090', getToken: () => 't', fetchImpl })), fetchImpl };
  }

  it('checks for the bundle, uploads it as gzip and downloads outputs', async () => {
    const { gw, fetchImpl } = api((url, init) => {
      if (url.endsWith('/bundles/abc')) {
        return new Response('{"error":"Bundle not found","code":"not_found"}', { status: 404 });
      }
      if (url.endsWith('/bundles') && init.method === 'POST') {
        return new Response('{"id":"abc","size_bytes":3}', { status: 201 });
      }
      return new Response(new Uint8Array([1, 2, 3]), { status: 200 });
    });
    expect(await gw.hasBundle('abc')).toBe(false);
    expect(await gw.uploadBundle(new Uint8Array([1, 2, 3]))).toEqual({ id: 'abc', sizeBytes: 3 });
    const upload = fetchImpl.mock.calls[1][1] as RequestInit;
    expect((upload.headers as Record<string, string>)['Content-Type']).toBe('application/gzip');
    expect([...(await gw.downloadTaskOutputs('e 1', 't/2'))]).toEqual([1, 2, 3]);
    expect(fetchImpl.mock.calls[2][0]).toBe('http://m:8090/executions/e%201/tasks/t%2F2/outputs');
  });

  it('passes other errors on instead of treating them as a missing bundle', async () => {
    const { gw } = api(() => new Response('{"error":"down"}', { status: 502 }));
    await expect(gw.hasBundle('abc')).rejects.toThrow();
  });
});
